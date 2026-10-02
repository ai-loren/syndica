"""捕获即回放（M3 T14）：脚本化的 CLI run 捕获下来，回放出逐块相同的轨迹、逐项相同的闸门判定与
同样的结局；以错误结束的、被 Ctrl-C 打断的 run 也回放到同一处；Agent 之间的先后靠真实耗时决定的
run（消息晚几轮才被注入、kill 落在子 Agent 跑命令的半路上）也一样。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest

import syndica.cli.host as host_module
from syndica.cli.host import RuntimeCliHost
from syndica.cli.main import CliIO, main
from syndica.kernel import (
    ContextBlock,
    FileTraceStore,
    LLMConfig,
    LLMResponseError,
    RuleRedactor,
    Runtime,
    SpanStatus,
    SyndicaError,
    ToolCall,
    TraceHeader,
    parse_scenario_document,
)
from syndica.kernel.tracing import current_llm_call
from syndica.runner.replay import ReplayResult, ReplayRunner

_WITHIN = 30.0

Step = (
    ContextBlock | Callable[[Sequence[str]], ContextBlock | Awaitable[ContextBlock]] | BaseException
)


def _token(seed: int) -> str:
    rng = random.Random(seed)
    return 'ghp_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=36))


def _calls(*calls: tuple[str, str, dict[str, object]]) -> ContextBlock:
    return ContextBlock(
        role='assistant',
        tool_calls=[
            ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))
            for call_id, name, arguments in calls
        ],
    )


def _say(text: str) -> ContextBlock:
    return ContextBlock(role='assistant', content=text)


class _PerAgent:
    """按 Agent 给回复：第一个来要回复的是根，第二个是它派出的子 Agent。一步可以是函数，按此刻已经
    知道的 Agent id 现写参数（子 Agent 的 id 在 run 之前不知道）。"""

    def __init__(self, *scripts: list[Step]) -> None:
        self.config = LLMConfig(provider='test', model='per-agent')
        self._scripts = scripts
        self._agents: list[str] = []
        self._next: dict[str, int] = {}

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        key = current_llm_call()
        assert key is not None
        agent_id = key[0]
        if agent_id not in self._agents:
            self._agents.append(agent_id)
        position = self._next.get(agent_id, 0)
        self._next[agent_id] = position + 1
        step = self._scripts[self._agents.index(agent_id)][position]
        if isinstance(step, BaseException):
            raise step
        answer = step if isinstance(step, ContextBlock) else step(self._agents)
        return await answer if inspect.isawaitable(answer) else answer


def _slow(seconds: float, step: Step) -> Step:
    """这一步的模型要想这么久才答：Agent 之间谁先谁后由它决定。"""

    async def answer(agents: Sequence[str]) -> ContextBlock:
        await asyncio.sleep(seconds)
        given = step if isinstance(step, ContextBlock) else cast(Any, step)(agents)
        return await given if inspect.isawaitable(given) else given

    return answer


class _Snapshotting(Runtime):
    """CLI 关掉 Runtime 的那一刻记下每个 Agent 的轨迹：原始 run 那一侧的比对对象。"""

    seen: list[dict[str, list[ContextBlock]]] = []

    async def shutdown(self) -> None:
        await super().shutdown()
        if getattr(self, '_snapshotted', False):
            return
        self._snapshotted = True
        trajectories: dict[str, list[ContextBlock]] = {}
        for info in await self.registry.list():
            try:
                trajectories[info.agent_id] = list((await self.wait(info.agent_id)).current_traj)
            except SyndicaError:
                continue
        _Snapshotting.seen.append(trajectories)


def _cli(tmp_path: Path, llm: Any, argv: list[str], *, stdin: str = '') -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    io = CliIO(stdin=StringIO(stdin), stdout=stdout, stderr=stderr)
    host = RuntimeCliHost(
        io=io,
        model_factory=lambda _: llm,
        workspace_root=tmp_path,
        environ={key: value for key, value in os.environ.items() if not key.startswith('GIT_')},
    )
    try:
        code = main(argv, host=host, io=io)
    finally:
        host.close(interrupted=False)
    return code, stdout.getvalue(), stderr.getvalue()


def _trace(root: Path) -> tuple[TraceHeader, dict[str, Any]]:
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read() -> tuple[TraceHeader, str | None]:
        [header] = await store.traces()
        return header, await store.replay(header.trace_id)

    header, text = asyncio.run(read())
    assert text is not None, 'the trace has no replay document'
    return header, cast(dict[str, Any], json.loads(text))


def _replayed(
    header: TraceHeader,
    document: dict[str, Any],
    *,
    perturb: Callable[[], Awaitable[None]] | None = None,
) -> ReplayResult:
    rules = RuleRedactor(rules=header.redaction.rules)
    return ReplayRunner(redactor=rules, perturb=perturb).run(
        parse_scenario_document(document, source='replay.json')
    )


def _noise(seed: int) -> Callable[[], Awaitable[None]]:
    """回放里每次外部调用之前多让出几次事件循环：Agent 之间的先后跟着变。"""
    rng = random.Random(seed)

    async def perturb() -> None:
        for _ in range(rng.randint(0, 6)):
            await asyncio.sleep(0)

    return perturb


def _selectors(document: dict[str, Any]) -> dict[str, str]:
    """agent id → 选择器（`角色[第几个]`），按回放文档里实例的顺序。"""
    ids = {tuple(item['key']): item['id'] for item in document['ids']}
    return {
        ids[tuple(instance['key'])]: f'{role}[{index}]'
        for role, body in document['agents'].items()
        for index, instance in enumerate(body['instances'])
    }


def _blocks(blocks: Sequence[ContextBlock], redactor: RuleRedactor) -> list[tuple[object, ...]]:
    """逐块比的那几项：`role`、`content`（原始 run 那一侧按同一套规则脱敏）、`trust`、工具调用按
    解析后的参数比；`extra` 不比。"""

    def text(value: object) -> object:
        return str(redactor.redact(value)) if isinstance(value, str) else value

    return [
        (
            block.role,
            text(block.content),
            block.trust.value,
            [(call.id, call.name, json.loads(call.arguments)) for call in block.tool_calls],
            block.tool_call_id,
        )
        for block in blocks
    ]


def _assert_same_trajectories(
    document: dict[str, Any], replayed: ReplayResult, original: dict[str, list[ContextBlock]]
) -> set[str]:
    selectors = _selectors(document)
    redactor = RuleRedactor()
    compared: set[str] = set()
    for agent_id, blocks in original.items():
        selector = selectors[agent_id]
        if selector not in replayed.trajectories:
            continue
        assert _blocks(replayed.trajectories[selector], redactor) == _blocks(blocks, redactor), (
            selector
        )
        compared.add(selector)
    return compared


@pytest.fixture
def snapshots(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, list[ContextBlock]]]:
    _Snapshotting.seen = []
    monkeypatch.setattr(host_module, 'Runtime', _Snapshotting)
    return _Snapshotting.seen


def test_a_run_with_every_kind_of_record_replays_block_for_block(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    token = _token(11)
    (tmp_path / 'notes.txt').write_text(f'deploy key: {token}\n', encoding='utf-8')
    root: list[Step] = [
        _calls(('c1', 'Bash', {'command': 'cat /etc/hosts'})),
        _calls(('q1', 'AskUser', {'question': 'colour?'})),
        _calls(('c2', 'Bash', {'command': 'git commit -m wip'})),
        _calls(('c3', 'Bash', {'command': 'sudo ls'})),
        _calls(('r1', 'Read', {'path': 'missing.txt'})),
        _calls(('r2', 'Read', {'path': 'notes.txt'})),
        _calls(('d1', 'Delegate', {'agent': 'Explorer', 'query': 'look around'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _say('all done'),
    ]
    child: list[Step] = [
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[0], 'content': 'halfway'}),
        ),
        _say('child done'),
    ]

    code, stdout, stderr = _cli(
        tmp_path,
        _PerAgent(root, child),
        ['run', 'go', '--agent', 'Worker'],
        stdin='approve\nblue\n',
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0 and stdout.rstrip().endswith('all done'), stderr
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert 'Worker[0]' in _assert_same_trajectories(document, replayed, original)
    [lead] = document['agents']['Worker']['instances']
    dispositions = {gate['disposition'] for gate in lead['gate']}
    assert dispositions >= {'allow', 'confirm', 'block'}
    assert [item['call'][1] for item in lead['skipped']] == [3]
    assert {response['kind'] for response in document['responses']} == {
        'confirmation',
        'user_input',
    }
    assert len(document['agents']['Explorer']['instances']) == 1
    assert document['agents']['Orchestrator']['frozen']['impl'] == (
        'syndica.kernel.agents.orchestrator:Orchestrator'
    )
    assert document['agents']['Worker']['frozen']['impl'] is None
    assert token not in json.dumps(document)


def _woken_child_reports_late() -> tuple[list[Step], list[Step]]:
    """根叫醒做完活的子 Agent 再接着干一段耗时的活；子 Agent 的报告在那段活之后的一轮才被注入。
    回放里那段活不花时间，报告不按记录的那一轮注入，根的请求就与记录不同。"""
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Explorer', 'query': 'say hi'})),
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'please report back'})
        ),
        _calls(('b1', 'Bash', {'command': 'sleep 0.8; echo working'})),
        _calls(('b2', 'Bash', {'command': 'echo more'})),
        _say('done'),
    ]
    child: list[Step] = [
        _say('hi'),
        lambda agents: _calls(
            ('s2', 'SendMessage', {'agent_id': agents[0], 'content': 'report: all good'})
        ),
        _say('reported'),
    ]
    return root, child


def test_a_message_injected_turns_after_it_was_sent_replays_into_the_same_turn(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    code, _, stderr = _cli(
        tmp_path, _PerAgent(*_woken_child_reports_late()), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)
    [lead] = document['agents']['Worker']['instances']
    [explorer] = document['agents']['Explorer']['instances']
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert [item['turn'] for item in lead['messages']] == [4]
    assert [item['turn'] for item in explorer['messages']] == [2]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {'Worker[0]', 'Explorer[0]'}


def test_a_message_injected_turns_after_it_was_sent_replays_the_same_way_under_noise(
    tmp_path: Path,
):
    code, _, stderr = _cli(
        tmp_path, _PerAgent(*_woken_child_reports_late()), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)

    results = [_replayed(header, document, perturb=_noise(seed)) for seed in range(8)]

    assert code == 0, stderr
    assert [result.divergences for result in results] == [()] * 8


def _kills_after(child_role: str, woken: list[Step]) -> tuple[list[Step], list[Step]]:
    """根把做完活的子 Agent 叫醒去干别的，自己干一段耗时的活，再把它 kill 掉。回放里那段活不花时间，
    kill 不等子 Agent 做完记录里它做过的那些就落下来。"""
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': child_role, 'query': 'say hi'})),
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'run the long job'})
        ),
        _calls(('b1', 'Bash', {'command': 'sleep 0.8'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _say('killed it'),
    ]
    return root, [_say('hi'), *woken]


@pytest.mark.parametrize(
    ('lead', 'child'), [(0.3, 0.0), (0.0, 0.3)], ids=['lead-slow', 'child-slow']
)
def test_a_message_whose_turn_came_from_model_latency_replays_into_the_same_turn(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]], lead: float, child: float
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Explorer', 'query': 'task A'})),
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'now do B'})
        ),
        _slow(lead, _say('thinking')),
        _slow(lead, _say('all done')),
    ]
    worker: list[Step] = [
        _say('A done'),
        _slow(
            child,
            lambda agents: _calls(
                ('w1', 'SendMessage', {'agent_id': agents[0], 'content': 'B result'})
            ),
        ),
        _say('B done'),
    ]

    code, _, stderr = _cli(tmp_path, _PerAgent(root, worker), ['run', 'go'])
    header, document = _trace(tmp_path)
    [orchestrator] = document['agents']['Orchestrator']['instances']
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert orchestrator.get('continued') == ([4] if lead else None)
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {
        'Orchestrator[0]',
        'Explorer[0]',
    }


def test_a_kill_that_landed_after_a_woken_cycle_replays_after_that_cycle(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    script = _kills_after('Explorer', [_say('woken and done')])

    code, stdout, stderr = _cli(tmp_path, _PerAgent(*script), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    [explorer] = document['agents']['Explorer']['instances']
    replayed = _replayed(header, document)

    assert code == 0 and stdout.rstrip().endswith('killed it'), stderr
    assert [(reply['turn'], reply['attempt']) for reply in explorer['turns']] == [(1, 1), (2, 1)]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {'Worker[0]', 'Explorer[0]'}


def test_a_child_killed_halfway_through_a_command_replays_the_kill_at_the_same_place(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    script = _kills_after('Worker', [_calls(('b2', 'Bash', {'command': 'sleep 30'}))])

    code, stdout, stderr = _cli(tmp_path, _PerAgent(*script), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    lead, child = document['agents']['Worker']['instances']
    replayed = _replayed(header, document)

    assert code == 0 and stdout.rstrip().endswith('killed it'), stderr
    assert [(reply['turn'], reply['attempt']) for reply in child['turns']] == [(1, 1), (2, 1)]
    assert child['tool_results'] == [] and len(lead['tool_results']) == 1
    assert child['cut'] == [[2, 2, 0]]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {'Worker[0]', 'Worker[1]'}


def test_a_kill_that_landed_while_its_target_waited_in_delegate_replays_with_that_delegate_cut(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(
            ('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'hand the long job on'})
        ),
        _calls(('b1', 'Bash', {'command': 'sleep 0.3'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _calls(('b2', 'Bash', {'command': 'sleep 1.2'})),
        _say('killed it'),
    ]
    child: list[Step] = [
        _say('hi'),
        _calls(('d2', 'Delegate', {'agent': 'Worker', 'query': 'long job'})),
        _say('never'),
    ]
    grandchild: list[Step] = [_calls(('g1', 'Bash', {'command': 'sleep 0.8'})), _say('job done')]

    code, stdout, stderr = _cli(
        tmp_path, _PerAgent(root, child, grandchild), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)
    _, killed, _ = document['agents']['Worker']['instances']
    replayed = _replayed(header, document)

    assert code == 0 and stdout.rstrip().endswith('killed it'), stderr
    assert killed['cut'] == [[2, 2, 0]]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {
        'Worker[0]',
        'Worker[1]',
        'Worker[2]',
    }


def test_a_message_sent_right_after_a_kill_wakes_the_killed_child_in_replay_too(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'go on'})),
        _calls(('b1', 'Bash', {'command': 'sleep 0.4'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _slow(
            0.05,
            lambda agents: _calls(
                ('s2', 'SendMessage', {'agent_id': agents[1], 'content': 'resume'})
            ),
        ),
        _calls(('b2', 'Bash', {'command': 'sleep 0.4'})),
        _say('done'),
    ]
    child: list[Step] = [
        _say('hi'),
        _calls(('c1', 'Bash', {'command': 'sleep 30'})),
        _say('resumed'),
    ]

    code, _, stderr = _cli(tmp_path, _PerAgent(root, child), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    _, killed = document['agents']['Worker']['instances']
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert [item['turn'] for item in killed['messages']] == [2, 3]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {'Worker[0]', 'Worker[1]'}


def test_a_deferred_message_and_a_kill_replay_into_the_cycle_after_the_kill(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'go on'})),
        _calls(('b1', 'Bash', {'command': 'sleep 0.2'})),
        lambda agents: _calls(
            (
                's2',
                'SendMessage',
                {'agent_id': agents[1], 'content': 'later', 'trigger': 'deferred'},
            )
        ),
        _calls(('b2', 'Bash', {'command': 'sleep 0.8'})),
        lambda agents: _calls(('k1', 'KillAgent', {'agent_id': agents[1]})),
        _slow(
            0.05,
            lambda agents: _calls(
                ('s3', 'SendMessage', {'agent_id': agents[1], 'content': 'resume'})
            ),
        ),
        _calls(('b3', 'Bash', {'command': 'sleep 0.4'})),
        _say('done'),
    ]
    child: list[Step] = [
        _say('hi'),
        _calls(('c1', 'Bash', {'command': 'sleep 0.5'})),
        _calls(('c2', 'Bash', {'command': 'sleep 30'})),
        _say('resumed'),
    ]

    code, _, stderr = _cli(tmp_path, _PerAgent(root, child), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    _, killed = document['agents']['Worker']['instances']
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert [(item['turn'], len(item['ids'])) for item in killed['messages']] == [(2, 1), (4, 2)]
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {'Worker[0]', 'Worker[1]'}


def test_a_message_that_came_after_a_delegated_child_answered_leaves_that_answer_alone(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'go on'})),
        _calls(('d2', 'Delegate', {'agent': 'Worker', 'query': 'answer once'})),
        _calls(('b1', 'Bash', {'command': 'sleep 1.0'})),
        _say('done'),
    ]
    sender: list[Step] = [
        _say('hi'),
        _calls(('m1', 'Bash', {'command': 'sleep 0.5'})),
        lambda agents: _calls(('m2', 'SendMessage', {'agent_id': agents[2], 'content': 'more'})),
        _say('sent'),
    ]
    answerer: list[Step] = [
        _calls(('a1', 'Bash', {'command': 'sleep 0.1'})),
        _say('first answer'),
        _say('second answer'),
    ]

    code, _, stderr = _cli(
        tmp_path, _PerAgent(root, sender, answerer), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {
        'Worker[0]',
        'Worker[1]',
        'Worker[2]',
    }
    [delegated] = [
        block
        for block in replayed.trajectories['Worker[0]']
        if block.role == 'tool' and block.tool_call_id == 'd2'
    ]
    assert 'first answer' in str(delegated.content)


def test_a_kill_still_pending_when_the_root_finished_lands_before_the_run_shuts_down(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'go on'})),
        _calls(('b1', 'Bash', {'command': 'sleep 1.5'})),
        _say('done'),
    ]
    killer: list[Step] = [
        _say('hi'),
        _calls(('d2', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('k1', 'SendMessage', {'agent_id': agents[2], 'content': 'work'})),
        _calls(('k2', 'Bash', {'command': 'sleep 0.3'})),
        lambda agents: _calls(('k3', 'KillAgent', {'agent_id': agents[2]})),
        _slow(
            0.05,
            lambda agents: _calls(
                ('k4', 'SendMessage', {'agent_id': agents[2], 'content': 'resume'})
            ),
        ),
        _calls(('k5', 'Bash', {'command': 'sleep 0.3'})),
        _say('killer done'),
    ]
    target: list[Step] = [
        _say('target hi'),
        _calls(('t1', 'Bash', {'command': 'sleep 30'})),
        _say('target resumed'),
    ]

    code, _, stderr = _cli(
        tmp_path, _PerAgent(root, killer, target), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert _assert_same_trajectories(document, replayed, original) == {
        'Worker[0]',
        'Worker[1]',
        'Worker[2]',
    }


def test_a_message_the_root_never_took_in_stays_out_of_it_however_early_it_arrives(
    tmp_path: Path,
):
    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry later')
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'report'})),
        _slow(0.4, failure),
    ]
    child: list[Step] = [
        _say('hi'),
        _slow(
            0.05,
            lambda agents: _calls(
                ('c1', 'SendMessage', {'agent_id': agents[0], 'content': 'too late'})
            ),
        ),
        _say('reported'),
    ]

    code, _, _ = _cli(tmp_path, _PerAgent(root, child), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    lead, _ = document['agents']['Worker']['instances']
    results = [
        _replayed(header, document),
        *(_replayed(header, document, perturb=_noise(seed)) for seed in range(8)),
    ]

    assert code != 0
    assert lead['messages'] == []
    assert [result.divergences for result in results] == [()] * 9


def _fan_in(*, second_sender_waits: float, receiver_works: float) -> list[list[Step]]:
    """根派出三个子 Agent，把它们都叫醒；前两个各干一段活再给第三个发消息，第三个自己也在干活，
    两条消息在它干完之后的那一轮才被注入。"""
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        _calls(('d2', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        _calls(('d3', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s3', 'SendMessage', {'agent_id': agents[3], 'content': 'wait'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'tell'})),
        lambda agents: _calls(('s2', 'SendMessage', {'agent_id': agents[2], 'content': 'tell'})),
        _calls(('b1', 'Bash', {'command': f'sleep {receiver_works + 0.6}'})),
        _say('done'),
    ]

    def sender(name: str, seconds: float) -> list[Step]:
        return [
            _say(f'{name} hi'),
            _calls((f'{name}1', 'Bash', {'command': f'sleep {seconds}'})),
            lambda agents: _calls(
                (f'{name}2', 'SendMessage', {'agent_id': agents[3], 'content': f'from {name}'})
            ),
            _say(f'{name} done'),
        ]

    receiver: list[Step] = [
        _say('receiver hi'),
        _calls(('r1', 'Bash', {'command': f'sleep {receiver_works}'})),
        _say('got them'),
    ]
    return [root, sender('a', 0.5), sender('b', second_sender_waits), receiver]


def test_two_messages_injected_into_one_turn_replay_in_their_recorded_order_under_noise(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    scripts = _fan_in(second_sender_waits=0.2, receiver_works=0.9)

    code, _, stderr = _cli(tmp_path, _PerAgent(*scripts), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    *_, receiver = document['agents']['Worker']['instances']
    ids = {tuple(item['key']): item['id'] for item in document['ids']}
    agents = [ids[tuple(instance['key'])] for instance in document['agents']['Worker']['instances']]
    sent_by = {
        item['id']: agents.index(item['key'][1])
        for item in document['ids']
        if item['key'][-2] == 'message' and item['key'][1] in agents[1:3]
    }
    results = [
        _replayed(header, document),
        *(_replayed(header, document, perturb=_noise(seed)) for seed in range(8)),
    ]

    assert code == 0, stderr
    assert [(item['turn'], [sent_by.get(i) for i in item['ids']]) for item in receiver['messages']][
        -1
    ] == (3, [2, 1])
    assert [result.divergences for result in results] == [()] * 9
    [original] = snapshots
    assert len(_assert_same_trajectories(document, results[0], original)) == 4


def test_messages_from_two_senders_to_a_woken_child_replay_into_the_same_turns_under_noise(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    root: list[Step] = [
        _calls(('d1', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        _calls(('d2', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        _calls(('d3', 'Delegate', {'agent': 'Worker', 'query': 'say hi'})),
        lambda agents: _calls(('s3', 'SendMessage', {'agent_id': agents[3], 'content': 'idle'})),
        lambda agents: _calls(('s2', 'SendMessage', {'agent_id': agents[2], 'content': 'tell'})),
        lambda agents: _calls(('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'tell'})),
        _calls(('b1', 'Bash', {'command': 'sleep 1.8'})),
        _say('done'),
    ]
    first: list[Step] = [
        _say('a hi'),
        *[_calls((f'a{i}', 'Bash', {'command': 'true'})) for i in range(6)],
        _calls(('a8', 'Bash', {'command': 'sleep 0.4'})),
        lambda agents: _calls(('a9', 'SendMessage', {'agent_id': agents[3], 'content': 'M1'})),
        _say('a done'),
    ]
    second: list[Step] = [
        _say('b hi'),
        _calls(('b1', 'Bash', {'command': 'sleep 0.8'})),
        lambda agents: _calls(('b2', 'SendMessage', {'agent_id': agents[3], 'content': 'M2'})),
        _say('b done'),
    ]
    receiver: list[Step] = [
        _say('receiver hi'),
        *[_calls((f'r{i}', 'Bash', {'command': 'true'})) for i in range(4)],
        _say('idle'),
        _calls(('r9', 'Bash', {'command': 'sleep 1.0'})),
        _say('got both'),
    ]

    code, _, stderr = _cli(
        tmp_path, _PerAgent(root, first, second, receiver), ['run', 'go', '--agent', 'Worker']
    )
    header, document = _trace(tmp_path)
    *_, woken = document['agents']['Worker']['instances']
    results = [
        _replayed(header, document),
        *(_replayed(header, document, perturb=_noise(seed)) for seed in range(8)),
    ]

    assert code == 0, stderr
    assert [item['turn'] for item in woken['messages']] == [2, 7, 8]
    assert [result.divergences for result in results] == [()] * 9
    [original] = snapshots
    assert len(_assert_same_trajectories(document, results[0], original)) == 4


def test_a_read_only_run_replays_with_its_refused_writes(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    (tmp_path / 'notes.txt').write_text('plain notes\n', encoding='utf-8')
    root: list[Step] = [
        _calls(('r1', 'Read', {'path': 'notes.txt'})),
        _calls(('w1', 'Write', {'path': 'out.txt', 'content': 'x'})),
        _calls(('q1', 'AskUser', {'question': 'ok?'})),
        _say('done'),
    ]

    code, _, stderr = _cli(
        tmp_path, _PerAgent(root), ['run', 'go', '--agent', 'Worker', '--no-write'], stdin='yes\n'
    )
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code == 0, stderr
    assert not (tmp_path / 'out.txt').exists()
    assert document['host']['no_write'] is True
    assert replayed.reproduced, replayed.divergences
    [original] = snapshots
    assert 'Worker[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_run_that_ends_in_an_error_replays_the_same_error(
    tmp_path: Path, snapshots: list[dict[str, list[ContextBlock]]]
):
    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry later')
    root: list[Step] = [_calls(('r1', 'Read', {'path': 'missing.txt'})), failure]

    code, _, _ = _cli(tmp_path, _PerAgent(root), ['run', 'go', '--agent', 'Worker'])
    header, document = _trace(tmp_path)
    replayed = _replayed(header, document)

    assert code != 0
    assert header.status is SpanStatus.ERROR
    assert document['expect'] == {'stop_reasons': {'Worker[0]': 'error'}}
    assert replayed.reproduced, replayed.divergences
    assert replayed.result.stop_reasons == {'Worker[0]': 'error'}


def test_a_secret_in_what_a_tool_ran_with_replays_with_only_the_user_rules():
    from syndica.kernel import (
        AgentOptions,
        BaseAgent,
        EventBus,
        HooksCenter,
        InMemoryTraceStore,
        ScriptedLLM,
        ScriptedTurn,
        TraceRecorder,
        local_tools,
    )
    from syndica.kernel.local_workspace import LocalWorkspace

    secret = 'sk-' + ''.join(random.Random(5).choices(string.ascii_letters + string.digits, k=40))

    async def capture(root: Path) -> tuple[InMemoryTraceStore, str]:
        store = InMemoryTraceStore()
        recorder = TraceRecorder(store, RuleRedactor(secrets=[secret]))
        await recorder.begin()
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        tools = local_tools(workspace.portal())
        llm = ScriptedLLM(
            [
                ScriptedTurn(
                    tool_calls=[
                        ToolCall(
                            id='c1',
                            name='Bash',
                            arguments=json.dumps({'command': f'echo {secret}'}),
                        )
                    ]
                ),
                ScriptedTurn(content='done'),
            ]
        )

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            options = AgentOptions(
                name='Lead', llm=llm, tools={'Bash': tools['Bash']}, system_prompt='You lead.'
            )
            return BaseAgent(options, hooks=hooks, events=events)

        runtime = Runtime(
            agents={'Lead': build}, orchestration=True, tracer=recorder, ids=recorder.ids
        )
        recorder.describe_run(
            host={'modules': []},
            roles={'Lead': {'system_prompt': 'You lead.', 'tools': ['Bash']}},
            entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        )
        try:
            agent_id = await runtime.spawn(build, query='go')
            await asyncio.wait_for(runtime.wait(agent_id), timeout=_WITHIN)
        finally:
            await runtime.shutdown()
            await workspace.close()
        await recorder.finish(SpanStatus.OK)
        return store, recorder.trace_id

    store, trace_id = asyncio.run(capture(Path(tempfile.mkdtemp())))

    async def read() -> str | None:
        return await store.replay(trace_id)

    text = asyncio.run(read())
    assert text is not None and secret not in text
    replayed = ReplayRunner().run(parse_scenario_document(json.loads(text), source='replay.json'))
    assert replayed.reproduced, replayed.divergences


def _kernel_capture(
    scripts: list[list[Step]],
    *,
    role: str = 'Lead',
    hook: Callable[[Any], None] | None = None,
    interrupt_when: Callable[[], Any] | None = None,
    entry: str = 'query',
    runtime: Mapping[str, Any] | None = None,
    agent: Mapping[str, Any] | None = None,
    host: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, list[ContextBlock]]]:
    """在内核里捕获一次 run（真的本地工具、开着编排、没有 Module），交回回放文档与原始轨迹。
    `interrupt_when` 是一个可等待的东西：等到它就像 Ctrl-C 那样打断这次 run；`entry='send'` 时根
    不带 query 起来，由宿主发一条消息开始。"""
    from syndica.kernel import (
        AgentOptions,
        BaseAgent,
        EventBus,
        HooksCenter,
        InMemoryTraceStore,
        TraceRecorder,
        local_tools,
    )
    from syndica.kernel.local_workspace import LocalWorkspace

    async def capture(root: Path) -> tuple[str, dict[str, list[ContextBlock]]]:
        store = InMemoryTraceStore()
        recorder = TraceRecorder(store, RuleRedactor())
        await recorder.begin()
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        tools = local_tools(workspace.portal())
        llm = _PerAgent(*scripts)

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            options = AgentOptions(
                name=role,
                llm=llm,
                tools={'Bash': tools['Bash']},
                system_prompt='You lead.',
                **dict(agent or {}),
            )
            built = BaseAgent(options, hooks=hooks, events=events)
            if hook is not None:
                hook(built)
            return built

        runtime_options = {'orchestration': True, **dict(runtime or {})}
        built_runtime = Runtime(
            agents={role: build}, tracer=recorder, ids=recorder.ids, **runtime_options
        )
        recorder.describe_run(
            host={'modules': []} if host is None else host,
            roles={role: {'system_prompt': 'You lead.', 'tools': ['Bash']}},
            entry={'mode': entry, 'agent': role, 'prompt': 'go'},
        )
        status = SpanStatus.OK
        try:
            if entry == 'query':
                agent_id = await built_runtime.spawn(build, query='go')
            else:
                agent_id = await built_runtime.spawn(build)
                assert (await built_runtime.send(agent_id, 'go')).ok
            if interrupt_when is None:
                await asyncio.wait_for(built_runtime.wait(agent_id), timeout=_WITHIN)
            else:
                await asyncio.wait_for(interrupt_when(), timeout=_WITHIN)
                recorder.interrupted()
                status = SpanStatus.CANCELLED
        finally:
            await built_runtime.shutdown()
            await workspace.close()
        trajectories = {
            info.agent_id: list((await built_runtime.wait(info.agent_id)).current_traj)
            for info in await built_runtime.registry.list()
        }
        await recorder.finish(status)
        text = await store.replay(recorder.trace_id)
        assert text is not None
        return text, trajectories

    return asyncio.run(capture(Path(tempfile.mkdtemp())))


def test_an_interrupt_after_a_tool_returned_but_before_its_result_landed_replays_there():
    landed = asyncio.Event()

    def slow_hook(agent: Any) -> None:
        from syndica.kernel import HookResult, Junction

        async def after(**_: Any) -> HookResult:
            landed.set()
            await asyncio.get_running_loop().create_future()
            return HookResult()

        agent.register_hook(Junction.POST_TOOL_USE, after)

    text, original = _kernel_capture(
        [[_calls(('c1', 'Bash', {'command': 'printf hi'})), _say('never')]],
        hook=slow_hook,
        interrupt_when=landed.wait,
    )
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    agent_id = next(item['id'] for item in document['ids'] if item['key'] == ['host', 'spawn', 0])
    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 1, 0]]}
    ]
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_run_entered_by_a_host_message_replays_without_a_record_of_that_message():
    text, original = _kernel_capture(
        [[_calls(('c1', 'Bash', {'command': 'printf hi'})), _say('done')]], entry='send'
    )
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    assert document['agents']['Lead']['instances'][0]['messages'] == []
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_an_interrupt_after_a_delegate_returned_but_before_its_result_landed_replays_there():
    landed = asyncio.Event()

    def slow_hook(agent: Any) -> None:
        from syndica.kernel import HookResult, Junction

        async def after(tool_call: ToolCall, **_: Any) -> HookResult:
            if tool_call.id == 'd2':
                landed.set()
                await asyncio.get_running_loop().create_future()
            return HookResult()

        agent.register_hook(Junction.POST_TOOL_USE, after)

    text, original = _kernel_capture(
        [
            [
                _calls(('d1', 'Delegate', {'agent': 'Lead', 'query': 'say hi'})),
                lambda agents: _calls(
                    ('s1', 'SendMessage', {'agent_id': agents[1], 'content': 'run'})
                ),
                _calls(('d2', 'Delegate', {'agent': 'Lead', 'query': 'say hi'})),
                _say('never'),
            ],
            [_say('helper hi'), _calls(('b1', 'Bash', {'command': 'sleep 30'})), _say('never')],
            [_say('hi')],
        ],
        hook=slow_hook,
        interrupt_when=landed.wait,
    )
    document = json.loads(text)
    lead, helper, _ = document['agents']['Lead']['instances']
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    assert lead['cut'] == [[1, 3, 0]]
    assert helper['cut'] == [[2, 2, 0]]
    [interrupt] = document['host_actions']
    assert [key[2:] for key in interrupt['waiting']] == [[2, 2, 0]]
    assert replayed.reproduced, replayed.divergences
    assert _assert_same_trajectories(document, replayed, original) == {
        'Lead[0]',
        'Lead[1]',
        'Lead[2]',
    }


def _risky(policy: object, **runtime: Any) -> tuple[str, dict[str, list[ContextBlock]]]:
    """一次跑在给定处置策略下的高风险命令（递归强删一个不存在的目录）：宿主档案照它写。"""
    from syndica.cli.capture import host_profile
    from syndica.kernel import DEFAULT_RULES, DangerJudge, HostFacts

    judge = DangerJudge(DEFAULT_RULES)
    options = {'orchestration': True, 'confirmation_timeout': None, **runtime}
    host = host_profile(
        flags={},
        judge=judge,
        policy=cast(Any, policy),
        output_limit=10_000,
        facts=HostFacts(os='posix', platform='test', shell='/bin/sh'),
        max_turns=None,
        no_write=False,
        modules=[],
        orchestration=cast(bool, options['orchestration']),
        confirmation_timeout=cast(Any, options['confirmation_timeout']),
    )
    return _kernel_capture(
        [[_calls(('c1', 'Bash', {'command': 'rm -rf ./nothing-here'})), _say('done')]],
        runtime={'orchestration': options['orchestration']},
        agent={'danger_judge': judge, 'danger_policy': policy},
        host=host,
    )


@pytest.mark.parametrize('kind', ['allow_all', 'block_high_risk', 'table'])
def test_a_run_under_any_built_in_policy_replays_under_the_same_one(kind: str):
    from syndica.kernel import AllowAll, BlockHighRisk, Disposition
    from syndica.kernel.danger import RuleTablePolicy

    policy = {
        'allow_all': AllowAll(),
        'block_high_risk': BlockHighRisk(),
        'table': RuleTablePolicy({'shell.recursive_force_remove': Disposition.ALLOW}),
    }[kind]

    text, original = _risky(policy)
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    [lead] = document['agents']['Lead']['instances']
    assert document['host']['danger']['policy'] == kind
    assert lead['gate'][0]['disposition'] == ('block' if kind == 'block_high_risk' else 'allow')
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_run_without_orchestration_replays_without_it():
    from syndica.kernel import BlockHighRisk

    text, original = _risky(BlockHighRisk(), orchestration=False)
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    assert document['host']['orchestration'] is False
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_run_without_a_confirmation_channel_replays_without_one():
    from syndica.kernel import Disposition
    from syndica.kernel.danger import RuleTablePolicy

    text, original = _risky(RuleTablePolicy({'shell.recursive_force_remove': Disposition.CONFIRM}))
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    [lead] = document['agents']['Lead']['instances']
    assert document['host']['confirmation_timeout'] is None
    assert lead['gate'][0]['disposition'] == 'confirm'
    assert replayed.reproduced, replayed.divergences
    assert 'Lead[0]' in _assert_same_trajectories(document, replayed, original)


def test_a_policy_the_replay_cannot_rebuild_is_named_when_the_run_is_described(
    caplog: pytest.LogCaptureFixture,
):
    from syndica.cli.capture import host_profile
    from syndica.kernel import (
        DEFAULT_RULES,
        DangerJudge,
        Disposition,
        HostFacts,
        InMemoryTraceStore,
        TraceRecorder,
    )

    class _Lenient:
        def decide(self, request: object) -> Disposition:
            return Disposition.ALLOW

    host = host_profile(
        flags={},
        judge=DangerJudge(DEFAULT_RULES),
        policy=cast(Any, _Lenient()),
        output_limit=10_000,
        facts=HostFacts(os='posix', platform='test', shell='/bin/sh'),
        max_turns=None,
        no_write=False,
        modules=[],
        orchestration=True,
        confirmation_timeout=None,
    )
    recorder = TraceRecorder(InMemoryTraceStore(), RuleRedactor())

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        recorder.describe_run(
            host=host,
            roles={'Lead': {'system_prompt': 'You lead.', 'tools': ['Bash']}},
            entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert record.levelno == logging.WARNING
    assert '_Lenient' in cast(tuple[str, str, str], record.args)[1]
    assert '不写回放文档' in cast(tuple[str, str, str], record.args)[2]


def test_a_role_named_like_a_secret_keeps_the_provider_ids_under_it():
    provider_id = 'call_' + ''.join(
        random.Random(9).choices(string.ascii_letters + string.digits, k=24)
    )

    text, original = _kernel_capture(
        [[_calls((provider_id, 'Bash', {'command': 'printf hi'})), _say('done')]],
        role='session-notes',
    )
    document = json.loads(text)
    replayed = ReplayRunner().run(parse_scenario_document(document, source='replay.json'))

    assert provider_id in text
    assert replayed.reproduced, replayed.divergences
    assert 'session-notes[0]' in _assert_same_trajectories(document, replayed, original)


_TOPOLOGY = """version: 1
topologies:
  solo:
    slots:
      - name: root
        agent: Worker
        is_root: true
        cardinality: singleton
        boot: eager
"""


def _without_document(root: Path) -> bool:
    store = FileTraceStore(root / '.syndica' / 'traces')

    async def read() -> str | None:
        [header] = await store.traces()
        return await store.replay(header.trace_id)

    return asyncio.run(read()) is None


@pytest.mark.parametrize('asked', [True, False], ids=['replay-asked-for', 'default-level'])
def test_a_topology_run_writes_no_replay_document_and_says_so_only_when_asked(
    tmp_path: Path, asked: bool
):
    config = tmp_path / 'syndica.yaml'
    config.write_text(_TOPOLOGY, encoding='utf-8')
    trace = ['--trace', 'replay'] if asked else []

    code, _, stderr = _cli(
        tmp_path,
        _PerAgent([_say('done')]),
        ['--config', str(config), 'run', 'go', '--topology', 'solo', *trace],
    )

    assert code == 0, stderr
    assert _without_document(tmp_path)
    assert ('trace 里不写回放文档：--topology 的 run 还不能捕获' in stderr) is asked


@pytest.mark.parametrize('asked', [True, False], ids=['replay-asked-for', 'default-level'])
def test_a_custom_class_based_role_writes_no_replay_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asked: bool
):
    real = host_module.frozen_roles

    def with_a_custom_role(catalog: Any, factories: Any) -> dict[str, dict[str, object]]:
        roles = real(catalog, factories)
        roles['Worker'] = {**roles['Worker'], 'impl': 'acme.roles:Custom'}
        return roles

    monkeypatch.setattr(host_module, 'frozen_roles', with_a_custom_role)
    trace = ['--trace', 'replay'] if asked else []

    code, _, stderr = _cli(
        tmp_path, _PerAgent([_say('done')]), ['run', 'go', '--agent', 'Worker', *trace]
    )

    assert code == 0, stderr
    assert _without_document(tmp_path)
    assert ('角色 Worker 是自定义的类式角色' in stderr) is asked


def test_a_role_that_cannot_be_built_costs_only_the_replay_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def broken(catalog: Any, factories: Any) -> dict[str, dict[str, object]]:
        raise RuntimeError('constructor needs a network')

    monkeypatch.setattr(host_module, 'frozen_roles', broken)

    code, stdout, stderr = _cli(
        tmp_path, _PerAgent([_say('done')]), ['run', 'go', '--agent', 'Worker']
    )

    assert code == 0 and stdout.rstrip().endswith('done'), stderr
    assert _without_document(tmp_path)
    assert 'constructor needs a network' in stderr


# --- Ctrl-C: a real SIGINT to a CLI in a child process ------------------------------------

_DRIVER = """
import json
import sys
from pathlib import Path

import syndica.__main__ as entry
from syndica.kernel import ScriptedLLM, ScriptedTurn, ToolCall

spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
llm = ScriptedLLM(
    [
        ScriptedTurn(
            content=turn.get('content'),
            tool_calls=[
                ToolCall(id=call['id'], name=call['name'], arguments=json.dumps(call['arguments']))
                for call in turn.get('tool_calls', [])
            ],
        )
        for turn in spec['turns']
    ]
)
entry._configured_model = lambda name: llm
sys.argv = ['syndica', *spec['argv']]
entry.entrypoint()
"""


def _drain(stream: Any, into: list[bytes]) -> None:
    for chunk in iter(lambda: stream.read1(4096), b''):
        into.append(chunk)


def test_a_run_interrupted_by_ctrl_c_replays_to_the_same_place(tmp_path: Path):
    root = tmp_path / 'work'
    root.mkdir()
    waiting = 'echo $$ > cut.pid && touch cut.ready && exec sleep 60'
    turns = [
        {'tool_calls': [{'id': 'r1', 'name': 'Read', 'arguments': {'path': 'missing.txt'}}]},
        {'tool_calls': [{'id': 'b1', 'name': 'Bash', 'arguments': {'command': waiting}}]},
    ]
    (tmp_path / 'driver.py').write_text(_DRIVER, encoding='utf-8')
    (tmp_path / 'spec.json').write_text(
        json.dumps({'argv': ['run', 'go', '--agent', 'Worker'], 'turns': turns}), encoding='utf-8'
    )
    environ = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    process = subprocess.Popen(
        [sys.executable, str(tmp_path / 'driver.py'), str(tmp_path / 'spec.json')],
        cwd=root,
        env={**environ, 'HOME': str(tmp_path)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, output)),
        threading.Thread(target=_drain, args=(process.stderr, output)),
    ]
    for reader in readers:
        reader.start()
    try:
        deadline = time.monotonic() + _WITHIN
        while not (root / 'cut.ready').exists():
            assert process.poll() is None, b''.join(output).decode('utf-8', 'replace')
            assert time.monotonic() < deadline, 'the agent command never started'
            time.sleep(0.05)
        process.send_signal(signal.SIGINT)
        code = process.wait(timeout=_WITHIN)
    finally:
        if process.poll() is None:
            process.kill()
        assert process.stdin is not None
        process.stdin.close()
        for reader in readers:
            reader.join(_WITHIN)

    header, document = _trace(root)
    replayed = _replayed(header, document)

    assert code == 130, b''.join(output).decode('utf-8', 'replace')
    assert header.status is SpanStatus.CANCELLED
    agent_id = next(item['id'] for item in document['ids'] if item['key'] == ['host', 'spawn', 0])
    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 2, 0]]}
    ]
    assert replayed.reproduced, replayed.divergences
