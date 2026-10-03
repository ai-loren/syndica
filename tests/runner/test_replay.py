"""回放执行（M3 D11）：外部世界按记录给出，内核全真并逐项比对。"""

from __future__ import annotations

import asyncio
import builtins
import copy
import hashlib
import logging
import os
import random
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel import (
    ConfigError,
    ContractViolation,
    EdgeKind,
    HookResult,
    InMemoryTraceStore,
    Junction,
    RedactionRule,
    RuleRedactor,
    SpanKind,
    SpanStatus,
    Subscription,
    TraceHeader,
    TraceLevel,
    TraceRecorder,
    TraceSource,
    parse_scenario_document,
)
from syndica.kernel.tools.orchestration import QueryAgent
from syndica.kernel.trace_recorder import RequestFingerprints, arguments_digest
from syndica.runner import replay as replay_module
from syndica.runner.replay import ReplayResult, ReplayRunner

_LEAD = 'a1' * 16
_HELPER = 'b2' * 16
_LEAD_KEY = ['host', 'spawn', 0]
_HOST_MESSAGE = {'key': ['host', 'message', 0], 'id': 'f9' * 16}
_ALLOWED = {
    'rule_ids': [],
    'risk': 'none',
    'provenance': 'trusted',
    'decided_by': 'policy',
    'disposition': 'allow',
}


def _allowed(*call: int, provenance: str = 'trusted') -> dict[str, Any]:
    return {'call': list(call), **_ALLOWED, 'provenance': provenance}


def _call(call_id: str, name: str, **arguments: object) -> dict[str, Any]:
    return {'id': call_id, 'name': name, 'arguments': arguments}


def _turn(turn: int, attempt: int = 1, **fields: object) -> dict[str, Any]:
    return {'turn': turn, 'attempt': attempt, **fields}


def _lead(*turns: dict[str, Any], tools: tuple[str, ...] = ('Bash', 'Read'), **instance: object):
    return {
        'frozen': {'system_prompt': 'You lead.', 'tools': list(tools)},
        'instances': [{'key': _LEAD_KEY, 'turns': list(turns), **instance}],
    }


def _doc(lead: dict[str, Any], *, captured: bool = True, **root: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        'version': 2,
        'name': 'captured',
        'agents': {'Lead': lead},
        'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        'ids': [{'key': _LEAD_KEY, 'id': _LEAD}],
        'expect': {},
    }
    if captured:
        document['source'] = {'trace_id': 'ab' * 16}
    document.update(root)
    return document


def _bash_run(output: str = 'a\nb', trust: str = 'untrusted') -> dict[str, Any]:
    return _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='ls')]),
            _turn(2, content='done'),
            tool_results=[{'call': [1, 1, 0], 'output': output, 'trust': trust}],
            gate=[_allowed(1, 1, 0)],
        ),
        expect={'final_output': {'Lead[0]': 'done'}, 'stop_reasons': {'Lead[0]': 'completed'}},
    )


def _replay(
    document: dict[str, Any], *, workspace_root: Path | None = None, **options: Any
) -> ReplayResult:
    return ReplayRunner(**options).run(
        parse_scenario_document(document, source='captured.json'), workspace_root=workspace_root
    )


def _kinds(result: ReplayResult) -> list[str]:
    return [divergence.kind for divergence in result.divergences]


def test_a_captured_run_replays_without_touching_the_outside_world(
    monkeypatch: pytest.MonkeyPatch,
):
    started: list[object] = []
    real_open = builtins.open

    def no_process(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError('回放起了子进程')

    def read_only_open(file: Any, mode: str = 'r', *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in 'wax+'):
            started.append((file, mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', no_process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', no_process)
    monkeypatch.setattr(builtins, 'open', read_only_open)

    result = _replay(_bash_run())

    assert result.reproduced, result.divergences
    assert result.result.tool_order == {'Lead[0]': ('Bash',)}
    assert started == []


_LEAD_SLOT_KEY = ['slot', 'lead', '', 0]
_HELPER_SLOT_KEY = ['slot', 'helper', '', 0]


def _peers(*, perturbed: bool = False) -> dict[str, Any]:
    send = _call('s1', 'SendMessage', agent_id=_HELPER, content='help', trigger='pending')
    return {
        'version': 2,
        'name': 'peers',
        'agents': {
            'Lead': {
                'frozen': {'system_prompt': 'You lead.', 'tools': ['Read']},
                'instances': [
                    {
                        'key': _LEAD_SLOT_KEY,
                        'turns': [_turn(1, tool_calls=[send]), _turn(2, content='lead done')],
                        'gate': [_allowed(1, 1, 0)],
                    }
                ],
            },
            'Helper': {
                'frozen': {'system_prompt': 'You help.', 'tools': ['Read']},
                'instances': [
                    {'key': _HELPER_SLOT_KEY, 'turns': [_turn(1, content='helper done')]}
                ],
            },
        },
        'topology': {
            'slots': [
                {
                    'name': 'lead',
                    'agent': 'Lead',
                    'is_root': True,
                    'cardinality': 'singleton',
                    'boot': 'eager',
                },
                {'name': 'helper', 'agent': 'Helper', 'cardinality': 'singleton', 'boot': 'eager'},
            ]
        },
        'entry': {'slot': 'lead', 'prompt': 'go'},
        'ids': [
            {'key': _LEAD_SLOT_KEY, 'id': _LEAD},
            {'key': _HELPER_SLOT_KEY, 'id': _HELPER},
            _HOST_MESSAGE,
            {'key': ['tool_call', _LEAD, 1, 1, 0, 'message', 0], 'id': 'e8' * 16},
        ],
        'expect': {'final_output': {'Lead[0]': 'lead done', 'Helper[0]': 'helper done'}},
    }


def test_two_agents_sharing_the_replay_model_each_get_their_own_replies():
    result = _replay(_peers())

    assert result.reproduced, result.divergences
    assert result.result.statuses == {'Lead[0]': 'zombie', 'Helper[0]': 'zombie'}


@pytest.mark.parametrize('seed', [1, 2, 3, 4, 5, 6])
def test_two_interleaved_agents_replay_the_same_way_under_scheduling_noise(seed: int):
    rng = random.Random(seed)

    async def noise() -> None:
        for _ in range(rng.randint(0, 4)):
            await asyncio.sleep(0)

    result = _replay(_peers(), perturb=noise)

    assert result.reproduced, result.divergences
    assert result.result.final_output == {'Lead[0]': 'lead done', 'Helper[0]': 'helper done'}


def _retried(modules: list[str], flags: dict[str, Any]) -> dict[str, Any]:
    failure = {'type': 'syndica.kernel.errors.LLMResponseError', 'message': 'HTTP 503'}
    return _doc(
        _lead(_turn(1, 1, raises=failure), _turn(1, 2, content='done')),
        host={'modules': modules, 'flags': flags},
        expect={'final_output': {'Lead[0]': 'done'}},
    )


def test_a_retry_is_served_by_its_own_attempt_key():
    result = _replay(_retried(['safety', 'human-interaction', 'retry'], {'llm_retry_attempts': 1}))

    assert result.reproduced, result.divergences


def test_without_the_recorded_retry_module_the_replay_diverges():
    result = _replay(_retried(['safety', 'human-interaction'], {}), allow_divergence=True)

    assert 'unconsumed' in _kinds(result) or 'outcome' in _kinds(result)
    assert not result.reproduced


def _interrupted(host_actions: list[dict[str, Any]]) -> dict[str, Any]:
    return _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='ls')]),
            tool_results=[{'call': [1, 1, 0], 'output': 'a', 'trust': 'untrusted'}],
            gate=[_allowed(1, 1, 0)],
        ),
        host_actions=host_actions,
    )


@pytest.mark.parametrize('kind', ['interrupt', 'shutdown'])
def test_a_call_the_original_never_finished_blocks_until_the_recorded_host_action(kind: str):
    started = time.monotonic()

    result = _replay(_interrupted([{'kind': kind}]))

    assert result.reproduced, result.divergences
    assert time.monotonic() - started < 30


def test_a_call_with_no_record_and_no_interruption_is_a_divergence():
    result = _replay(_interrupted([]))

    [divergence] = result.divergences
    assert divergence.kind == 'reply'
    assert divergence.where == 'Lead[0] 第 2 轮第 1 次调用'


def _digests(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **options: Any
) -> list[list[str]]:
    """跑一遍、记下回放自己算出的每一次请求的指纹（先给每一次填一个占位的，取指纹时交回同一个
    占位，比对因此不会停下）。"""
    placeholder = ['0' * 64]
    probe = copy.deepcopy(document)
    for reply in probe['agents']['Lead']['instances'][0]['turns']:
        reply['request_digests'] = placeholder
    seen: list[list[str]] = []
    original = RequestFingerprints.of

    def recording(self: RequestFingerprints, request: Any) -> list[str]:
        seen.append(original(self, request))
        return placeholder

    monkeypatch.setattr(RequestFingerprints, 'of', recording)
    _replay(probe, **options)
    monkeypatch.setattr(RequestFingerprints, 'of', original)
    return seen


def _fingerprinted(
    document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **options: Any
) -> dict[str, Any]:
    pinned = copy.deepcopy(document)
    replies = pinned['agents']['Lead']['instances'][0]['turns']
    for reply, digests in zip(replies, _digests(document, monkeypatch, **options), strict=True):
        reply['request_digests'] = digests
    return pinned


def test_a_request_that_matches_its_fingerprints_replays_cleanly(monkeypatch: pytest.MonkeyPatch):
    result = _replay(_fingerprinted(_bash_run(), monkeypatch))

    assert result.reproduced, result.divergences


@pytest.mark.parametrize(
    ('change', 'unit'),
    [
        (
            lambda d: d['agents']['Lead']['frozen'].__setitem__('system_prompt', 'You lead!'),
            'system',
        ),
        (
            lambda d: d['agents']['Lead']['frozen'].__setitem__('tools', ['Read', 'Bash']),
            '工具定义',
        ),
        (lambda d: d['entry'].__setitem__('prompt', 'go now'), '第 0 条历史'),
    ],
    ids=['system-prompt', 'tool-order', 'prompt'],
)
def test_the_first_request_unit_that_differs_is_reported_with_where(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[dict[str, Any]], None], unit: str
):
    document = _fingerprinted(_bash_run(), monkeypatch)
    change(document)

    result = _replay(document)

    [divergence] = result.divergences
    assert divergence.kind == 'request'
    assert divergence.where == 'Lead[0] 第 1 轮第 1 次调用'
    assert f'「{unit}」' in divergence.detail


def test_only_the_first_differing_unit_of_a_request_is_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    document = _fingerprinted(_bash_run(), monkeypatch)
    document['agents']['Lead']['frozen']['system_prompt'] = 'You lead!'
    document['agents']['Lead']['frozen']['tools'] = ['Read', 'Bash']

    [divergence] = _replay(document).divergences

    assert '「system」' in divergence.detail


def test_with_divergence_allowed_the_replay_keeps_going_and_reports_every_difference(
    monkeypatch: pytest.MonkeyPatch,
):
    document = _fingerprinted(_bash_run(), monkeypatch)
    document['agents']['Lead']['frozen']['system_prompt'] = 'You lead!'

    result = _replay(document, allow_divergence=True)

    assert _kinds(result) == ['request', 'request']
    assert result.result.final_output == {'Lead[0]': 'done'}


def test_tool_results_that_change_the_history_show_up_in_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
):
    document = _fingerprinted(_bash_run(), monkeypatch)
    document['agents']['Lead']['instances'][0]['tool_results'][0]['output'] = 'something else'

    result = _replay(document)

    [divergence] = result.divergences
    assert (divergence.kind, divergence.where) == ('request', 'Lead[0] 第 2 轮第 1 次调用')


@pytest.mark.parametrize(
    ('change', 'where'),
    [
        (
            lambda d: d['agents']['Lead']['instances'][0]['turns'].append(_turn(3, content='more')),
            'Lead[0]',
        ),
        (
            lambda d: (
                d['agents']['Lead']['instances'][0]['turns'][0]['tool_calls'].append(
                    _call('c2', 'Bash', command='rm -rf /')
                ),
                d['agents']['Lead']['instances'][0]['tool_results'].append(
                    {'call': [1, 1, 1], 'output': 'never ran'}
                ),
            ),
            'Lead[0] [1, 1, 1]',
        ),
        (
            lambda d: d['ids'].append({'key': ['host', 'spawn', 9], 'id': 'dd' * 16}),
            "ids ['host', 'spawn', 9]",
        ),
    ],
    ids=['reply', 'tool-result', 'id'],
)
def test_records_the_replay_never_used_are_divergences(change: Any, where: str | None):
    document = _bash_run()
    change(document)

    result = _replay(document, allow_divergence=True)

    assert 'unconsumed' in _kinds(result)
    if where is not None:
        assert any(d.kind == 'unconsumed' and d.where == where for d in result.divergences)


def test_a_call_the_original_skipped_is_skipped_again_with_its_recorded_result():
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='git push')]),
            _turn(2, content='done'),
            skipped=[{'call': [1, 1, 0], 'output': '宿主拦下了 git push'}],
        ),
        expect={'final_output': {'Lead[0]': 'done'}},
    )

    result = _replay(document)

    assert result.reproduced, result.divergences


def _gated(*, rules: list[dict[str, Any]], gate: dict[str, Any], **host: object) -> dict[str, Any]:
    return _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='rm -rf /')]),
            _turn(2, content='done'),
            gate=[{'call': [1, 1, 0], **gate}],
        ),
        host={
            'danger': {'rules': rules, 'table': {'shell.recursive_force_remove': 'block'}},
            **host,
        },
    )


_BLOCKED = {
    'rule_ids': ['shell.recursive_force_remove'],
    'risk': 'high',
    'provenance': 'trusted',
    'decided_by': 'policy',
    'disposition': 'block',
}


def test_a_gate_decision_that_matches_the_record_replays_cleanly():
    result = _replay(_gated(rules=[{'id': 'shell.recursive_force_remove'}], gate=_BLOCKED))

    assert result.reproduced, result.divergences


def test_a_judge_missing_a_rule_shows_up_as_a_gate_divergence():
    result = _replay(
        _gated(rules=[{'id': 'shell.raw_device_write'}], gate=_BLOCKED), allow_divergence=True
    )

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert divergence.where == 'Lead[0] [1, 1, 0]'
    assert "rule_ids: 记录 ['shell.recursive_force_remove']，回放 []" in divergence.detail


def test_a_recorded_disposition_that_differs_is_named_in_the_gate_divergence():
    gate = {**_BLOCKED, 'disposition': 'confirm'}

    result = _replay(
        _gated(rules=[{'id': 'shell.recursive_force_remove'}], gate=gate), allow_divergence=True
    )

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert "disposition: 记录 'confirm'，回放 'block'" in divergence.detail


def _tainted(trust: str, flags: dict[str, Any] | None = None) -> dict[str, Any]:
    gate = {**_BLOCKED, 'provenance': 'untrusted', 'decided_by': 'flag'}
    return _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='curl x')]),
            _turn(2, tool_calls=[_call('c2', 'Bash', command='rm -rf /')]),
            _turn(3, content='done'),
            tool_results=[{'call': [1, 1, 0], 'output': 'downloaded', 'trust': trust}],
            gate=[{'call': [1, 1, 0], **_ALLOWED}, {'call': [1, 2, 0], **gate}],
        ),
        host={
            'danger': {
                'rules': [{'id': 'shell.recursive_force_remove'}],
                'table': {'shell.recursive_force_remove': 'confirm'},
            },
            'flags': {} if flags is None else flags,
        },
    )


def test_an_untrusted_mark_that_survives_the_replay_reproduces_the_escalation():
    result = _replay(_tainted('untrusted'))

    assert result.reproduced, result.divergences


def test_a_dropped_untrusted_mark_shows_up_as_a_gate_divergence():
    result = _replay(_tainted('trusted'), allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert "provenance: 记录 'untrusted'，回放 'trusted'" in divergence.detail


def test_the_untrusted_high_risk_flag_from_the_host_profile_takes_effect():
    allowed = _tainted('untrusted', flags={'untrusted_high_risk': 'allow'})
    allowed['agents']['Lead']['instances'][0]['gate'][1].update(disposition='allow')
    allowed['agents']['Lead']['instances'][0]['tool_results'].append(
        {'call': [1, 2, 0], 'output': 'removed'}
    )

    result = _replay(allowed)

    assert result.reproduced, result.divergences


def _outside(real: str | None) -> dict[str, Any]:
    params = {'root': '/syndica-replay-root'}
    if real is not None:
        params['real'] = real
    return _doc(
        _lead(
            _turn(
                1, tool_calls=[_call('c1', 'Bash', command='cat /private/syndica-replay-root/f')]
            ),
            _turn(2, content='done'),
            tool_results=[{'call': [1, 1, 0], 'output': 'x'}],
            gate=[
                {
                    'call': [1, 1, 0],
                    'rule_ids': [],
                    'risk': 'none',
                    'provenance': 'trusted',
                    'decided_by': 'policy',
                    'disposition': 'allow',
                }
            ],
        ),
        host={'danger': {'rules': [{'id': 'shell.path_outside_workspace', 'params': params}]}},
    )


def test_the_recorded_resolution_of_the_workspace_root_is_used_on_the_replay_machine():
    result = _replay(_outside('/private/syndica-replay-root'))

    assert result.reproduced, result.divergences


def test_resolving_the_root_on_the_replay_machine_would_have_changed_the_verdict():
    result = _replay(_outside(None), allow_divergence=True)

    assert 'gate' in _kinds(result)


def test_a_write_under_no_write_without_a_recorded_skip_is_a_divergence():
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Write', path='a.txt', content='x')]),
            _turn(2, content='done'),
            tools=('Write', 'Read'),
        ),
        host={'no_write': True},
    )

    result = _replay(document, allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'skip']
    assert divergence.where == 'Lead[0] Write [1, 1, 0]'


def _manifest_role(prompt: str = 'You lead.', turns: int = 3) -> dict[str, Any]:
    return {
        'manifest': {'name': 'Lead', 'description': 'leads', 'system_prompt': prompt},
        'instances': [{'turns': [{'content': f'turn {n}'} for n in range(1, turns + 1)]}],
    }


def _capped(max_turns: int | None) -> dict[str, Any]:
    host: dict[str, Any] = {} if max_turns is None else {'max_turns': max_turns}
    return {
        'version': 2,
        'name': 'capped',
        'host': host,
        'agents': {
            'Lead': {
                'manifest': {'name': 'Lead', 'description': 'leads', 'tools': ['Read']},
                'instances': [
                    {
                        'key': _LEAD_KEY,
                        'turns': [_turn(1, tool_calls=[_call('r1', 'Read', path='a.txt')])],
                        'tool_results': [{'call': [1, 1, 0], 'output': 'contents'}],
                    }
                ],
            }
        },
        'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
        'ids': [{'key': _LEAD_KEY, 'id': _LEAD}],
        'expect': {'stop_reasons': {'Lead[0]': 'max_turns'}},
    }


def test_the_host_max_turns_caps_roles_loaded_from_a_manifest():
    assert _replay(_capped(1)).reproduced
    [divergence] = _replay(_capped(None)).divergences
    assert (divergence.kind, divergence.where) == ('reply', 'Lead[0] 第 2 轮第 1 次调用')


def test_the_host_danger_table_decides_the_disposition():
    confirming = _gated(rules=[{'id': 'shell.recursive_force_remove'}], gate=_BLOCKED)
    confirming['host']['danger']['table'] = {'shell.recursive_force_remove': 'confirm'}

    result = _replay(confirming, allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert "disposition: 记录 'block'，回放 'confirm'" in divergence.detail


def test_the_host_output_limit_reaches_the_real_tools(monkeypatch: pytest.MonkeyPatch):
    def built(limit: int | None) -> dict[str, Any]:
        host: dict[str, Any] = {} if limit is None else {'output_limit': limit}
        return _doc(
            _lead(
                _turn(1, tool_calls=[_call('c1', 'Bash', command='printf 0123456789abcdef')]),
                _turn(2, content='done'),
            ),
            captured=False,
            host=host,
        )

    pinned = _fingerprinted(built(4), monkeypatch, real_tools=True)
    unlimited = copy.deepcopy(pinned)
    del unlimited['host']['output_limit']

    assert _replay(pinned, real_tools=True).reproduced
    [divergence] = _replay(unlimited, real_tools=True).divergences
    assert (divergence.kind, divergence.where) == ('request', 'Lead[0] 第 2 轮第 1 次调用')


def test_host_facts_reach_the_system_prompt_of_manifest_roles(monkeypatch: pytest.MonkeyPatch):
    def built(os_name: str) -> dict[str, Any]:
        return {
            'version': 2,
            'name': 'facts',
            'host': {'host_facts': {'os': os_name, 'platform': 'arm64', 'shell': '/bin/sh'}},
            'agents': {
                'Lead': {
                    'manifest': {
                        'name': 'Lead',
                        'description': 'leads',
                        'system_prompt': 'On {os}.',
                    },
                    'instances': [{'turns': [_turn(1, content='done')]}],
                }
            },
            'entry': {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
            'expect': {},
        }

    pinned = built('linux')
    pinned['agents']['Lead']['instances'][0]['turns'][0]['request_digests'] = _digests(
        built('linux'), monkeypatch
    )[0]
    replayed = copy.deepcopy(pinned)
    replayed['host']['host_facts']['os'] = 'darwin'

    assert _replay(pinned).reproduced
    [divergence] = _replay(replayed).divergences
    assert '「system」' in divergence.detail


def _asked(response: dict[str, Any] | None, actions: list[dict[str, Any]] | None = None):
    request = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)]),
            _turn(2, content='done'),
            tools=('Read',),
            gate=[_allowed(1, 1, 0)],
        ),
        responses=[] if response is None else [{'key': request, **response}],
        host_actions=actions or [],
    )
    if response is not None and response['kind'] != 'timeout':
        document['ids'].append({'key': [*request, 'reply'], 'id': 'cc' * 16})
    return document, request


def test_a_human_answer_is_given_by_the_key_of_its_request():
    document, _ = _asked({'kind': 'user_input', 'answer': 'blue'})

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_recorded_timeout_elapses_without_real_waiting_and_without_a_false_error(
    caplog: pytest.LogCaptureFixture,
):
    document, _ = _asked({'kind': 'timeout'})
    document['agents']['Lead']['instances'][0]['tool_results'] = []
    started = time.monotonic()

    with caplog.at_level(logging.ERROR):
        result = _replay(document)

    assert result.reproduced, result.divergences
    assert time.monotonic() - started < 30
    assert [r for r in caplog.records if r.name == 'syndica.kernel.runtime'] == []


def test_an_abort_at_its_request_ends_the_next_turn_aborted():
    document, request = _asked(None, [{'kind': 'abort', 'key': None}])
    document['host_actions'] = [{'kind': 'abort', 'key': request}]
    document['agents']['Lead']['instances'][0]['turns'] = [
        _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)])
    ]
    document['expect'] = {'stop_reasons': {'Lead[0]': 'aborted'}}

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_recorded_error_keeps_its_type_name():
    document = _doc(
        _lead(_turn(1, raises={'type': 'openai.RateLimitError', 'message': 'slow down'})),
        expect={
            'errors': {'Lead[0]': 'RateLimitError: slow down'},
            'stop_reasons': {'Lead[0]': 'error'},
        },
    )

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_payload_the_replay_cannot_read_makes_the_document_unusable():
    document = _bash_run()
    document['agents']['Lead']['frozen']['system_prompt'] = {'payload': 'a' * 64}

    with pytest.raises(ConfigError) as caught:
        _replay(document)

    assert 'a' * 64 in caught.value.expected
    assert 'payloads=' in caught.value.next_action


def test_payloads_are_read_through_the_given_lookup():
    document = _bash_run()
    digest = hashlib.sha256(b'You lead.').hexdigest()
    document['agents']['Lead']['frozen']['system_prompt'] = {'payload': digest}

    result = _replay(document, payloads={digest: 'You lead.'}.get)

    assert result.reproduced, result.divergences


def test_a_v1_document_is_pointed_at_the_scenario_runner():
    document = {
        'version': 1,
        'name': 'v1',
        'agents': {
            'Root': {
                'manifest': {'name': 'Root', 'description': 'root'},
                'instances': [{'turns': [{'content': 'done'}]}],
            }
        },
        'topology': {
            'slots': [
                {
                    'name': 'root',
                    'agent': 'Root',
                    'is_root': True,
                    'cardinality': 'singleton',
                    'boot': 'eager',
                }
            ]
        },
        'entry': {'slot': 'root', 'prompt': 'go'},
        'responses': [],
        'expect': {},
    }

    with pytest.raises(ConfigError) as caught:
        _replay(document)

    assert 'ScenarioRunner' in caught.value.next_action


async def test_the_replay_model_refuses_a_call_outside_the_kernels_count():
    replay = object.__new__(replay_module._Replay)  # pyright: ignore[reportPrivateUsage]
    llm = replay_module._ReplayLLM(replay)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ConfigError) as caught:
        await llm.create([])

    assert '当前 LLM 调用' in caught.value.expected
    assert '不在内核的计数里' in caught.value.received


async def test_a_replayed_tool_refuses_to_run_outside_a_kernel_tool_call():
    document = parse_scenario_document(_bash_run(), source='x.json')
    replay = replay_module._Replay(  # pyright: ignore[reportPrivateUsage]
        ReplayRunner(), document, stubbed=True, virtual=True, resolved={}
    )

    with pytest.raises(ContractViolation) as caught:
        await replay.outcome('Bash', {'command': 'ls'})

    assert '当前工具调用' in caught.value.received


def test_hand_written_documents_without_tool_results_run_the_real_tools(tmp_path: Path):
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('w1', 'Write', path='made.txt', content='real')]),
            _turn(2, content='done'),
            tools=('Write', 'Read'),
        ),
        captured=False,
        expect={'workspace': {'made.txt': 'real'}},
    )

    result = _replay(document, workspace_root=tmp_path, real_tools=True)

    assert result.reproduced, result.divergences
    assert (tmp_path / 'made.txt').read_text() == 'real'
    assert os.path.exists(tmp_path)


def test_a_tool_that_raised_raises_again_and_its_error_reaches_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
):
    raised = _bash_run()
    raised['agents']['Lead']['instances'][0]['tool_results'] = [
        {'call': [1, 1, 0], 'raised': {'type': 'builtins.OSError', 'message': 'disk full'}}
    ]
    pinned = _fingerprinted(raised, monkeypatch)
    succeeded = copy.deepcopy(pinned)
    succeeded['agents']['Lead']['instances'][0]['tool_results'] = [
        {'call': [1, 1, 0], 'output': 'disk full', 'trust': 'untrusted'}
    ]

    replayed = _replay(pinned)
    [divergence] = _replay(succeeded).divergences

    assert replayed.reproduced, replayed.divergences
    [block] = [b for b in replayed.trajectories['Lead[0]'] if b.role == 'tool']
    assert 'OSError' in str(block.content) and 'disk full' in str(block.content)
    assert (divergence.kind, divergence.where) == ('request', 'Lead[0] 第 2 轮第 1 次调用')


def _two_questions() -> dict[str, Any]:
    first = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]
    second = ['tool_call', _LEAD, 1, 2, 0, 'request', 0]
    return _doc(
        _lead(
            _turn(1, tool_calls=[_call('q1', 'AskUser', question='colour?', timeout=300)]),
            _turn(2, tool_calls=[_call('q2', 'AskUser', question='size?', timeout=300)]),
            _turn(3, content='done'),
            tools=('Read',),
            gate=[_allowed(1, 1, 0), _allowed(1, 2, 0)],
        ),
        responses=[
            {'key': second, 'kind': 'user_input', 'answer': 'large'},
            {'key': first, 'kind': 'user_input', 'answer': 'blue'},
        ],
        ids=[
            {'key': _LEAD_KEY, 'id': _LEAD},
            {'key': [*first, 'reply'], 'id': 'c1' * 16},
            {'key': [*second, 'reply'], 'id': 'c2' * 16},
        ],
    )


def test_each_human_answer_goes_to_the_request_whose_key_it_carries():
    result = _replay(_two_questions())

    trajectory = result.trajectories['Lead[0]']
    answers = [block.content for block in trajectory if block.role == 'tool']
    assert result.reproduced, result.divergences
    assert len(answers) == 2
    assert 'blue' in str(answers[0]) and 'large' not in str(answers[0])
    assert 'large' in str(answers[1])


def test_an_interrupt_waits_until_every_record_has_been_used():
    request = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('q1', 'AskUser', question='colour?', timeout=300)]),
            _turn(2, tool_calls=[_call('c1', 'Bash', command='ls')]),
            tool_results=[{'call': [1, 2, 0], 'output': 'a'}],
            tools=('Bash', 'Read'),
            gate=[_allowed(1, 1, 0), _allowed(1, 2, 0)],
        ),
        responses=[{'key': request, 'kind': 'timeout'}],
        host_actions=[{'kind': 'interrupt'}],
    )

    result = _replay(document)

    assert result.reproduced, result.divergences
    assert result.result.tool_order == {}


def test_a_replay_that_can_never_finish_is_reported_as_stalled():
    document = _interrupted([{'kind': 'interrupt'}])
    document['responses'] = [
        {'key': ['tool_call', _LEAD, 1, 9, 0, 'request', 0], 'kind': 'user_input', 'answer': 'x'}
    ]
    started = time.monotonic()

    result = _replay(document)

    assert [d.kind for d in result.divergences] == ['stall']
    assert time.monotonic() - started < 30


def test_a_gate_decision_the_record_does_not_have_is_a_divergence():
    document = _tainted('untrusted')
    del document['agents']['Lead']['instances'][0]['gate'][0]

    result = _replay(document, allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert divergence.where == 'Lead[0] [1, 1, 0]'
    assert divergence.detail.startswith('回放里多出一次记录里没有的闸门判定')
    assert "disposition='allow'" in divergence.detail


def test_the_recorded_event_sequence_is_compared_when_the_document_states_it():
    document = _bash_run()
    document['expect']['events'] = ['agent_start', 'turn_start', 'turn_end', 'agent_end']

    result = _replay(document, allow_divergence=True)

    [divergence] = result.divergences
    assert divergence.where == 'expect.events'


def test_a_request_with_more_units_than_its_record_is_a_divergence(
    monkeypatch: pytest.MonkeyPatch,
):
    pinned = _fingerprinted(_bash_run(), monkeypatch)
    turns = pinned['agents']['Lead']['instances'][0]['turns']
    turns[1]['request_digests'] = turns[1]['request_digests'][:-1]

    [divergence] = _replay(pinned).divergences

    assert (divergence.kind, divergence.where) == ('request', 'Lead[0] 第 2 轮第 1 次调用')
    assert '记录有' in divergence.detail and '个单元' in divergence.detail


def test_an_untrusted_reply_makes_its_own_calls_untrusted_in_the_replay_too():
    document = _gated(
        rules=[{'id': 'shell.recursive_force_remove'}],
        gate={**_BLOCKED, 'provenance': 'untrusted', 'decided_by': 'flag'},
    )
    document['agents']['Lead']['instances'][0]['turns'][0]['trust'] = 'untrusted'

    result = _replay(document)

    assert result.reproduced, result.divergences


# --- review round: interruptions, quiescence, the end of the run ----------------------------


def test_queryagent_installed_by_the_runtime_is_replayed_from_its_record(
    monkeypatch: pytest.MonkeyPatch,
):
    async def refuse(self: QueryAgent, agent_id: str) -> Any:
        raise AssertionError('回放跑了真的 QueryAgent')

    monkeypatch.setattr(QueryAgent, 'execute', refuse)
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('q1', 'QueryAgent', agent_id=_LEAD)]),
            _turn(2, content='done'),
            tool_results=[{'call': [1, 1, 0], 'output': 'Lead is thinking'}],
            gate=[_allowed(1, 1, 0)],
        ),
    )

    result = _replay(document)

    assert result.reproduced, result.divergences
    [block] = [b for b in result.trajectories['Lead[0]'] if b.role == 'tool']
    assert block.content == 'Lead is thinking'


def _pending(call: dict[str, Any], waiting: list[Any] | None, **host: Any) -> dict[str, Any]:
    """原始 run 在第 1 轮的这次调用还没有结局时被 Ctrl-C：文档里没有它的结局与答复。"""
    action: dict[str, Any] = {'kind': 'interrupt'}
    if waiting is not None:
        action['waiting'] = waiting
    return _doc(
        _lead(_turn(1, tool_calls=[call]), gate=[host.pop('gate', _allowed(1, 1, 0))]),
        host=host,
        host_actions=[action],
    )


_CONFIRMING = {
    'danger': {
        'rules': [{'id': 'shell.recursive_force_remove'}],
        'table': {'shell.recursive_force_remove': 'confirm'},
    },
    'gate': {**_BLOCKED, 'call': [1, 1, 0], 'disposition': 'confirm'},
}


_REQUEST_ONE = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]


@pytest.mark.parametrize('positioned', [False, True], ids=['anywhere-quiet', 'at-its-waits'])
@pytest.mark.parametrize(
    ('call', 'host', 'waiting'),
    [
        (_call('c1', 'Bash', command='sleep 600'), {}, ['tool_call', _LEAD, 1, 1, 0]),
        (_call('q1', 'AskUser', question='colour?', timeout=300), {}, _REQUEST_ONE),
        (_call('c1', 'Bash', command='rm -rf /'), _CONFIRMING, _REQUEST_ONE),
    ],
    ids=['during-a-tool-call', 'while-a-question-waits', 'while-a-confirmation-waits'],
)
def test_an_interrupt_in_the_middle_of_a_call_replays_at_that_call(
    call: dict[str, Any], host: dict[str, Any], waiting: list[Any], positioned: bool
):
    document = _pending(call, [waiting] if positioned else None, **copy.deepcopy(host))
    if call['name'] == 'AskUser':
        document['agents']['Lead']['frozen']['tools'] = ['Read']

    result = _replay(document)

    assert result.reproduced, result.divergences


_CHILD = 'c7' * 16
_CHILD_KEY = ['tool_call', _LEAD, 1, 1, 0, 'spawn', 0]
_RETRYING = {
    'modules': ['safety', 'human-interaction', 'retry'],
    'flags': {'llm_retry_attempts': 1, 'llm_retry_backoff_ms': 2000},
}
_FAILED = {'type': 'syndica.kernel.errors.LLMResponseError', 'message': 'HTTP 503'}


def _woken_then_killed(*woken: dict[str, Any], **root: Any) -> dict[str, Any]:
    """根派出一个子 Agent，等它做完，再发消息叫醒它，然后 kill 它；`woken` 是子 Agent 被叫醒之后的
    那个周期在原始 run 里被 kill 之前留下的回复。"""
    lead = _lead(
        _turn(1, tool_calls=[_call('d1', 'Delegate', agent='Child', query='say hi')]),
        _turn(2, tool_calls=[_call('s1', 'SendMessage', agent_id=_CHILD, content='go on')]),
        _turn(3, tool_calls=[_call('k1', 'KillAgent', agent_id=_CHILD)]),
        _turn(4, content='lead done'),
        tools=('Read',),
        gate=[_allowed(1, 1, 0), _allowed(1, 2, 0), _allowed(1, 3, 0)],
    )
    document = _doc(lead, **root)
    document['agents']['Child'] = {
        'frozen': {'system_prompt': 'You help.', 'tools': ['Bash']},
        'instances': [{'key': _CHILD_KEY, 'turns': [_turn(1, content='hi'), *woken]}],
    }
    document['ids'] += [
        {'key': _CHILD_KEY, 'id': _CHILD},
        {'key': ['tool_call', _LEAD, 1, 2, 0, 'message', 0], 'id': 'e9' * 16},
    ]
    return document


def _killed() -> dict[str, Any]:
    """子 Agent 被叫醒之后的第一次模型调用还在飞时就被 kill 了：那一次没有回复。"""
    return _woken_then_killed()


def _expecting_a_message_never_sent() -> dict[str, Any]:
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('r1', 'Read', path='notes.txt')]),
            _turn(2, content='done'),
            tool_results=[{'call': [1, 1, 0], 'output': 'notes', 'trust': 'untrusted'}],
            gate=[_allowed(1, 1, 0)],
            messages=[{'turn': 2, 'ids': ['d4' * 16]}],
        ),
        expect={'final_output': {'Lead[0]': 'done'}},
    )
    document['ids'].append({'key': ['host', 'message', 7], 'id': 'd4' * 16})
    return document


def test_a_message_the_record_injected_that_the_replay_never_sends_diverges_where_it_was_due():
    stopped = _replay(_expecting_a_message_never_sent())
    carried_on = _replay(_expecting_a_message_never_sent(), allow_divergence=True)

    assert _kinds(stopped) == ['message']
    assert stopped.divergences[0].where == 'Lead[0] 第 2 轮'
    assert 'd4' * 16 in stopped.divergences[0].detail
    assert _kinds(carried_on) == ['message', 'unconsumed']
    assert carried_on.result.final_output == {'Lead[0]': 'done'}


def test_a_kill_waits_for_its_target_to_use_up_records_that_only_come_after_a_backoff():
    document = _woken_then_killed(
        _turn(2, 1, raises=_FAILED), _turn(2, 2, content='child done'), host=_RETRYING
    )

    result = _replay(document)

    assert result.reproduced, result.divergences
    assert result.result.final_output == {'Lead[0]': 'lead done', 'Child[0]': 'child done'}


def test_a_kill_whose_target_can_never_use_up_its_records_lands_anyway_and_reports_them():
    document = _woken_then_killed(_turn(2, content='child done'), _turn(3, content='never reached'))

    result = _replay(document)

    assert _kinds(result) == ['unconsumed']
    assert result.divergences[0].where == 'Child[0]'


@pytest.mark.parametrize('second', ['ran', 'skipped'])
def test_a_kill_waits_through_a_backoff_for_what_its_target_did_after_its_delegate_returned(
    second: str,
):
    grandchild_key = ['tool_call', _CHILD, 2, 2, 0, 'spawn', 0]
    document = _woken_then_killed(
        _turn(
            2,
            tool_calls=[
                _call('d2', 'Delegate', agent='Child', query='retry once'),
                _call('b2', 'Bash', command='echo after'),
            ],
        ),
        host=_RETRYING,
    )
    child = document['agents']['Child']['instances'][0]
    child['gate'] = [_allowed(2, 2, 0)]
    if second == 'ran':
        child['gate'].append(_allowed(2, 2, 1))
        child['tool_results'] = [{'call': [2, 2, 1], 'output': 'after'}]
    else:
        child['skipped'] = [{'call': [2, 2, 1], 'output': 'the host refused it'}]
    document['agents']['Child']['instances'].append(
        {
            'key': grandchild_key,
            'turns': [_turn(1, 1, raises=_FAILED), _turn(1, 2, content='gc done')],
        }
    )
    document['ids'].append({'key': grandchild_key, 'id': '9d' * 16})

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_message_the_record_injected_before_a_kill_that_never_comes_is_not_cut_off_by_it():
    document = _woken_then_killed(_turn(2, tool_calls=[_call('b2', 'Bash', command='echo step')]))
    child = document['agents']['Child']['instances'][0]
    child.update(
        tool_results=[{'call': [2, 2, 0], 'output': 'step'}],
        gate=[_allowed(2, 2, 0)],
        messages=[{'turn': 2, 'ids': ['e9' * 16]}, {'turn': 3, 'ids': ['d5' * 16]}],
    )
    document['ids'].append({'key': ['host', 'message', 7], 'id': 'd5' * 16})

    result = _replay(document)

    assert _kinds(result) == ['message']
    assert result.divergences[0].where == 'Child[0] 第 3 轮'


def test_a_kill_lands_where_its_target_was_cut_off_even_with_records_left_for_after_it_wakes():
    document = _peers()
    document['agents']['Lead']['instances'][0].update(
        turns=[
            _turn(1, tool_calls=[_call('d1', 'Delegate', agent='Child', query='say hi')]),
            _turn(2, tool_calls=[_call('s1', 'SendMessage', agent_id=_CHILD, content='go on')]),
            _turn(3, tool_calls=[_call('k1', 'KillAgent', agent_id=_CHILD)]),
            _turn(4, tool_calls=[_call('s2', 'SendMessage', agent_id=_HELPER, content='after')]),
            _turn(5, tool_calls=[_call('s3', 'SendMessage', agent_id=_CHILD, content='resume')]),
            _turn(6, content='lead done'),
        ],
        gate=[_allowed(1, turn, 0) for turn in range(1, 6)],
    )
    document['agents']['Helper']['instances'][0]['messages'] = [{'turn': 1, 'ids': ['e6' * 16]}]
    document['agents']['Child'] = {
        'frozen': {'system_prompt': 'You help.', 'tools': ['Bash']},
        'instances': [
            {
                'key': _CHILD_KEY,
                'turns': [_turn(1, content='hi'), _turn(3, content='child resumed')],
                'messages': [{'turn': 2, 'ids': ['e9' * 16]}, {'turn': 3, 'ids': ['e5' * 16]}],
            }
        ],
    }
    document['ids'] = [item for item in document['ids'] if item['key'][0] != 'tool_call'] + [
        {'key': _CHILD_KEY, 'id': _CHILD},
        {'key': ['tool_call', _LEAD, 1, 2, 0, 'message', 0], 'id': 'e9' * 16},
        {'key': ['tool_call', _LEAD, 1, 4, 0, 'message', 0], 'id': 'e6' * 16},
        {'key': ['tool_call', _LEAD, 1, 5, 0, 'message', 0], 'id': 'e5' * 16},
    ]
    document['expect'] = {}

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_kill_of_a_child_that_had_already_stopped_goes_out_before_anyone_waits_on_it():
    document = _peers()
    document['agents']['Lead']['instances'][0].update(
        turns=[
            _turn(1, tool_calls=[_call('d1', 'Delegate', agent='Child', query='say hi')]),
            _turn(2, tool_calls=[_call('k1', 'KillAgent', agent_id=_CHILD)]),
            _turn(3, tool_calls=[_call('s1', 'SendMessage', agent_id=_HELPER, content='after')]),
            _turn(4, content='lead done'),
        ],
        gate=[_allowed(1, turn, 0) for turn in range(1, 4)],
    )
    document['agents']['Helper']['instances'][0]['messages'] = [{'turn': 1, 'ids': ['e6' * 16]}]
    document['agents']['Child'] = {
        'frozen': {'system_prompt': 'You help.', 'tools': ['Bash']},
        'instances': [{'key': _CHILD_KEY, 'turns': [_turn(1, content='hi')], 'messages': []}],
    }
    document['ids'] = [item for item in document['ids'] if item['key'][0] != 'tool_call'] + [
        {'key': _CHILD_KEY, 'id': _CHILD},
        {'key': ['tool_call', _LEAD, 1, 3, 0, 'message', 0], 'id': 'e6' * 16},
    ]
    document['expect'] = {}

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_kill_lands_while_its_target_waits_in_a_delegate_the_original_never_got_back():
    grandchild_key = ['tool_call', _CHILD, 2, 2, 0, 'spawn', 0]
    document = _woken_then_killed(
        _turn(2, tool_calls=[_call('d2', 'Delegate', agent='Child', query='wait for word')])
    )
    document['agents']['Lead']['instances'][0].update(
        turns=[
            _turn(1, tool_calls=[_call('d1', 'Delegate', agent='Child', query='say hi')]),
            _turn(2, tool_calls=[_call('s1', 'SendMessage', agent_id=_CHILD, content='go on')]),
            _turn(3, tool_calls=[_call('k1', 'KillAgent', agent_id=_CHILD)]),
            _turn(4, tool_calls=[_call('s2', 'SendMessage', agent_id='9d' * 16, content='word')]),
            _turn(5, content='lead done'),
        ],
        gate=[_allowed(1, turn, 0) for turn in range(1, 5)],
    )
    child = document['agents']['Child']['instances'][0]
    child.update(
        gate=[_allowed(2, 2, 0)],
        cut=[[2, 2, 0]],
        messages=[{'turn': 2, 'ids': ['e9' * 16]}],
    )
    document['agents']['Child']['instances'].append(
        {
            'key': grandchild_key,
            'turns': [
                _turn(1, tool_calls=[_call('b1', 'Bash', command='sleep 1')]),
                _turn(2, content='gc done'),
            ],
            'tool_results': [{'call': [1, 1, 0], 'output': ''}],
            'gate': [_allowed(1, 1, 0)],
            'messages': [{'turn': 2, 'ids': ['e3' * 16]}],
        }
    )
    document['ids'] += [
        {'key': grandchild_key, 'id': '9d' * 16},
        {'key': ['tool_call', _LEAD, 1, 4, 0, 'message', 0], 'id': 'e3' * 16},
    ]

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_kill_whose_target_never_gets_to_where_it_landed_goes_out_once_the_run_is_quiet():
    grandchild_key = ['tool_call', _CHILD, 2, 2, 0, 'spawn', 0]
    document = _woken_then_killed(
        _turn(2, tool_calls=[_call('d2', 'Delegate', agent='Child', query='never answers')]),
        _turn(3, content='after'),
    )
    child = document['agents']['Child']['instances'][0]
    child['gate'] = [_allowed(2, 2, 0)]
    document['agents']['Child']['instances'].append(
        {
            'key': grandchild_key,
            'turns': [_turn(1, tool_calls=[_call('b1', 'Bash', command='sleep 9')])],
            'gate': [_allowed(1, 1, 0)],
        }
    )
    document['ids'].append({'key': grandchild_key, 'id': '9d' * 16})

    result = _replay(document, allow_divergence=True)

    assert result.result.final_output['Lead[0]'] == 'lead done'
    assert ('unconsumed', 'Child[0]') in [(d.kind, d.where) for d in result.divergences]


def test_a_kill_that_cannot_land_does_not_wait_for_its_target():
    document = _peers()
    lead = document['agents']['Lead']['instances'][0]
    lead['turns'] = [
        _turn(1, tool_calls=[_call('s1', 'SendMessage', agent_id=_HELPER, content='start')]),
        _turn(2, tool_calls=[_call('k1', 'KillAgent', agent_id=_HELPER)]),
        _turn(3, tool_calls=[_call('s2', 'SendMessage', agent_id=_HELPER, content='more')]),
        _turn(4, content='lead done'),
    ]
    lead['gate'] = [_allowed(1, 1, 0), _allowed(1, 2, 0), _allowed(1, 3, 0)]
    document['agents']['Helper']['frozen']['tools'] = ['Bash']
    [helper] = document['agents']['Helper']['instances']
    helper.update(
        turns=[
            _turn(1, tool_calls=[_call('b1', 'Bash', command='sleep 1')]),
            _turn(2, content='helper done'),
        ],
        tool_results=[{'call': [1, 1, 0], 'output': ''}],
        gate=[_allowed(1, 1, 0)],
        messages=[{'turn': 1, 'ids': ['e8' * 16]}, {'turn': 2, 'ids': ['e7' * 16]}],
    )
    document['ids'].append({'key': ['tool_call', _LEAD, 1, 3, 0, 'message', 0], 'id': 'e7' * 16})

    result = _replay(document)

    assert result.reproduced, result.divergences


@pytest.mark.parametrize('seed', range(1, 41))
def test_a_child_killed_in_the_middle_of_a_call_replays_the_same_way_under_noise(seed: int):
    rng = random.Random(seed)

    async def noise() -> None:
        for _ in range(rng.randint(0, 4)):
            await asyncio.sleep(0)

    result = _replay(_killed(), perturb=noise)

    assert result.reproduced, result.divergences


@pytest.mark.parametrize('seed', range(7, 41))
def test_peers_that_finish_after_the_root_still_replay_under_noise(seed: int):
    rng = random.Random(seed)

    async def noise() -> None:
        for _ in range(rng.randint(0, 4)):
            await asyncio.sleep(0)

    result = _replay(_peers(), perturb=noise)

    assert result.reproduced, result.divergences
    assert result.result.final_output == {'Lead[0]': 'lead done', 'Helper[0]': 'helper done'}


def test_events_compared_per_agent_do_not_depend_on_how_the_agents_interleave():
    baseline = _replay(_peers())
    by_agent: dict[str, list[str]] = {}
    for event in baseline.result.events:
        by_agent.setdefault(event.agent, []).append(event.kind)
    rng = random.Random(15)

    async def noise() -> None:
        for _ in range(rng.randint(0, 4)):
            await asyncio.sleep(0)

    pinned = _peers()
    pinned['expect'] = {'events': by_agent}
    wrong = copy.deepcopy(pinned)
    wrong['expect']['events']['Lead[0]'] = wrong['expect']['events']['Lead[0]'][:-1]

    assert len(by_agent) == 2 and all(by_agent.values())
    assert _replay(pinned, perturb=noise).reproduced
    [divergence] = _replay(wrong).divergences
    assert divergence.where == 'expect.events.Lead[0]'


def test_an_interrupt_recorded_with_its_waits_fires_exactly_there():
    document = _interrupted([{'kind': 'interrupt', 'waiting': [['llm_call', _LEAD, 2, 1]]}])

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_an_interrupt_whose_waits_the_replay_never_reaches_is_a_stall():
    document = _interrupted([{'kind': 'interrupt', 'waiting': [['llm_call', _LEAD, 3, 1]]}])

    [divergence] = _replay(document).divergences

    assert (divergence.kind, divergence.where) == ('stall', 'host_actions interrupt')
    assert f"['llm_call', '{_LEAD}', 3, 1]" in divergence.detail
    assert f"['llm_call', '{_LEAD}', 2, 1]" in divergence.detail


def test_a_call_cut_after_it_returned_parks_on_its_record_until_the_interrupt():
    document = _interrupted([{'kind': 'interrupt', 'waiting': [['tool_call', _LEAD, 1, 1, 0]]}])

    result = _replay(document)

    assert result.reproduced, result.divergences
    assert [block.role for block in result.trajectories['Lead[0]']] == ['user', 'assistant']


def _hanging_after_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """让内核在一次工具调用之后永远不往下走（模拟一处挂住的回归）。"""
    prepared = replay_module._Replay._prepared  # pyright: ignore[reportPrivateUsage]

    def hanging(self: Any, role: str, agent: Any) -> Any:
        async def never(**_: Any) -> HookResult:
            await asyncio.get_running_loop().create_future()
            return HookResult()

        agent.register_hook(Junction.POST_TOOL_USE, never)
        return prepared(self, role, agent)

    monkeypatch.setattr(replay_module._Replay, '_prepared', hanging)  # pyright: ignore[reportPrivateUsage]


def test_a_regression_that_hangs_before_the_recorded_interrupt_point_is_not_reproduced(
    monkeypatch: pytest.MonkeyPatch,
):
    _hanging_after_tools(monkeypatch)
    document = _interrupted([{'kind': 'interrupt', 'waiting': [['llm_call', _LEAD, 2, 1]]}])

    [divergence] = _replay(document).divergences

    assert (divergence.kind, divergence.where) == ('stall', 'host_actions interrupt')


def test_a_run_stuck_on_nothing_the_record_lacks_is_reported_as_stalled(
    monkeypatch: pytest.MonkeyPatch,
):
    _hanging_after_tools(monkeypatch)
    started = time.monotonic()

    [divergence] = _replay(_bash_run()).divergences

    assert (divergence.kind, divergence.where) == ('stall', 'run')
    assert time.monotonic() - started < 30


def test_a_divergence_before_the_run_starts_stops_instead_of_hanging():
    document = _bash_run()
    document['entry'] = {'mode': 'send', 'agent': 'Lead', 'prompt': 'go'}
    started = time.monotonic()

    result = _replay(document)

    assert [(d.kind, d.where) for d in result.divergences] == [
        ('id', "message ['host', 'message', 0]")
    ]
    assert time.monotonic() - started < 30


def test_the_virtual_clock_jumps_exactly_to_each_timer():
    loop = replay_module._VirtualClockLoop()  # pyright: ignore[reportPrivateUsage]
    seen: list[float] = []

    async def main() -> None:
        running = asyncio.get_running_loop()
        running.call_later(5, lambda: seen.append(running.time()))
        running.call_later(7.5, lambda: seen.append(running.time()))
        await asyncio.sleep(1e15)

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()

    assert seen == [5.0, 7.5]


def test_timers_far_in_the_future_elapse_without_spinning():
    retried = _retried(
        ['safety', 'human-interaction', 'retry'],
        {'llm_retry_attempts': 1, 'llm_retry_backoff_ms': 60_000},
    )
    asked, _ = _asked({'kind': 'timeout'})
    asked['agents']['Lead']['instances'][0]['turns'][0]['tool_calls'][0]['arguments'] = {
        'question': 'which colour?',
        'timeout': 1e15,
    }
    started = time.monotonic()

    results = [_replay(retried), _replay(asked)]

    assert all(result.reproduced for result in results), [r.divergences for r in results]
    assert time.monotonic() - started < 30


def test_an_interrupt_after_a_recorded_timeout_waits_for_that_timeout():
    asked, request = _asked({'kind': 'timeout'})
    asked['agents']['Lead']['instances'][0]['turns'] = [
        _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)])
    ]
    asked['host_actions'] = [{'kind': 'interrupt', 'waiting': [['llm_call', _LEAD, 2, 1]]}]

    result = _replay(asked)

    assert result.reproduced, result.divergences
    assert request == ['tool_call', _LEAD, 1, 1, 0, 'request', 0]


def test_a_question_that_times_out_in_the_replay_but_not_in_the_record_is_a_divergence():
    asked, _ = _asked(None)
    asked['agents']['Lead']['instances'][0]['turns'] = [
        _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)]),
        _turn(2, content='done'),
    ]

    result = _replay(asked)

    assert [(d.kind, d.where) for d in result.divergences] == [('response', 'Lead[0] user_input')]
    assert '超时' in result.divergences[0].detail


def test_a_question_that_timed_out_no_longer_counts_as_waiting():
    asked, _ = _asked(None)
    asked['agents']['Lead']['instances'][0]['turns'] = [
        _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)])
    ]

    result = _replay(asked, allow_divergence=True)

    assert [(d.kind, d.where) for d in result.divergences] == [
        ('response', 'Lead[0] user_input'),
        ('reply', 'Lead[0] 第 2 轮第 1 次调用'),
    ]


# --- review round: what the replay refuses ----------------------------------------------------


def test_a_hand_written_document_without_tool_results_is_not_run_for_real_by_default():
    document = _doc(
        _lead(_turn(1, tool_calls=[_call('c1', 'Bash', command='touch x')]), _turn(2)),
        captured=False,
    )

    with pytest.raises(ConfigError) as caught:
        _replay(document)

    assert 'Bash' in caught.value.received
    assert 'real_tools=True' in caught.value.next_action


@pytest.mark.parametrize(
    ('options', 'actions', 'anchor'),
    [
        ({'allow_divergence': True}, [], 'allow_divergence'),
        ({}, [{'kind': 'interrupt'}], 'interrupt'),
    ],
    ids=['allow-divergence', 'interrupt'],
)
def test_real_tools_refuse_what_needs_the_virtual_clock(
    options: dict[str, Any], actions: list[dict[str, Any]], anchor: str
):
    document = _doc(
        _lead(_turn(1, tool_calls=[_call('c1', 'Bash', command='true')]), _turn(2)),
        captured=False,
        host_actions=actions,
    )

    with pytest.raises(ConfigError) as caught:
        _replay(document, real_tools=True, **options)

    assert anchor in caught.value.received


@pytest.mark.parametrize(
    ('lookup', 'anchor'),
    [
        (lambda digest: b'You lead.', 'bytes'),
        (lambda digest: 'something else', '对不上'),
    ],
    ids=['bytes', 'wrong-content'],
)
def test_a_payload_that_is_not_its_text_makes_the_document_unusable(
    lookup: Callable[[str], Any], anchor: str
):
    document = _bash_run()
    document['agents']['Lead']['instances'][0]['turns'][1]['content'] = {
        'payload': hashlib.sha256(b'done').hexdigest()
    }

    with pytest.raises(ConfigError) as caught:
        _replay(document, payloads=lookup)

    assert anchor in caught.value.received


def test_a_payload_missing_from_a_reply_is_refused_before_anything_runs():
    document = _bash_run()
    document['agents']['Lead']['instances'][0]['turns'][1]['content'] = {'payload': 'c' * 64}
    document['expect'] = {}

    with pytest.raises(ConfigError) as caught:
        _replay(document)

    assert 'c' * 64 in caught.value.expected


def test_a_failure_inside_the_replays_own_stand_ins_is_raised_not_blamed_on_the_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    def broken(self: Any, recorded: Any) -> Any:
        raise RuntimeError('replay bug')

    monkeypatch.setattr(replay_module._Replay, '_result', broken)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match='replay bug'):
        _replay(_bash_run())


# --- review round: the host profile and the records ------------------------------------------


def test_flags_of_modules_the_replay_cannot_rebuild_are_listed_not_passed_on():
    document = _retried(
        ['safety', 'human-interaction', 'retry', 'cli-guard'],
        {'llm_retry_attempts': 1, 'guard_mode': 'strict'},
    )

    result = _replay(document)

    assert result.reproduced, result.divergences
    assert result.unrebuilt == ('cli-guard', 'flags.guard_mode')


def test_a_captured_document_without_gate_records_still_has_its_gates_compared():
    captured = _bash_run()
    del captured['agents']['Lead']['instances'][0]['gate']
    written = copy.deepcopy(captured)
    del written['source']

    [divergence] = _replay(captured).divergences

    assert (divergence.kind, divergence.where) == ('gate', 'Lead[0] [1, 1, 0]')
    assert _replay(written).reproduced


def _confirmation(response: dict[str, Any], outcome: str, executed: bool) -> dict[str, Any]:
    request = ['tool_call', _LEAD, 1, 1, 0, 'request', 0]
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='rm -rf /')]),
            _turn(2, content='done'),
            gate=[{**_BLOCKED, 'call': [1, 1, 0], 'disposition': 'confirm', 'outcome': outcome}],
            tool_results=[{'call': [1, 1, 0], 'output': 'removed'}] if executed else [],
        ),
        host=copy.deepcopy({'danger': _CONFIRMING['danger']}),
        responses=[{'key': request, **response}],
    )
    if response['kind'] != 'timeout':
        document['ids'].append({'key': [*request, 'reply'], 'id': 'cc' * 16})
    return document


@pytest.mark.parametrize(
    ('response', 'outcome', 'executed'),
    [
        ({'kind': 'confirmation', 'answer': 'approve'}, 'ok', True),
        ({'kind': 'confirmation', 'answer': 'approve', 'trust': 'untrusted'}, 'rejected', False),
        ({'kind': 'timeout'}, 'timeout', False),
    ],
    ids=['approved', 'untrusted-approval-is-a-rejection', 'timed-out'],
)
def test_how_a_confirmation_ended_replays_from_its_record(
    response: dict[str, Any], outcome: str, executed: bool
):
    result = _replay(_confirmation(response, outcome, executed))

    assert result.reproduced, result.divergences


def test_a_confirmation_that_ended_differently_is_a_gate_divergence():
    document = _confirmation({'kind': 'timeout'}, 'rejected', False)

    [divergence] = _replay(document, allow_divergence=True).divergences

    assert "outcome: 记录 'rejected'，回放 'timeout'" in divergence.detail


@pytest.mark.parametrize(
    ('field', 'value', 'name'),
    [('risk', 'low', 'risk'), ('decided_by', 'flag', 'decided_by')],
    ids=['risk', 'decided-by'],
)
def test_each_recorded_gate_field_is_compared(field: str, value: str, name: str):
    gate = {**_BLOCKED, field: value}

    result = _replay(
        _gated(rules=[{'id': 'shell.recursive_force_remove'}], gate=gate), allow_divergence=True
    )

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert divergence.detail.startswith(f'{name}: 记录 {value!r}')


def test_an_untrusted_entry_makes_the_first_calls_untrusted_in_the_replay_too():
    document = _gated(
        rules=[{'id': 'shell.recursive_force_remove'}],
        gate={**_BLOCKED, 'provenance': 'untrusted', 'decided_by': 'flag'},
    )
    document['entry']['trust'] = 'untrusted'

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_executed_arguments_are_compared_with_their_recorded_digest():
    matching = _bash_run()
    outcome = matching['agents']['Lead']['instances'][0]['tool_results'][0]
    outcome['arguments_digest'] = arguments_digest({'command': 'ls'}, RuleRedactor())
    other = copy.deepcopy(matching)
    other['agents']['Lead']['instances'][0]['tool_results'][0]['arguments_digest'] = (
        arguments_digest({'command': 'rm -rf ~'}, RuleRedactor())
    )

    [divergence] = _replay(other).divergences

    assert _replay(matching).reproduced
    assert (divergence.kind, divergence.where) == ('tool', 'Lead[0] Bash [1, 1, 0]')
    assert '执行的参数' in divergence.detail


def test_fingerprints_use_the_rule_set_the_replay_is_given(monkeypatch: pytest.MonkeyPatch):
    rules = RuleRedactor(rules=[RedactionRule('custom.prompt', r'\bgo\b')])
    pinned = _fingerprinted(_bash_run(), monkeypatch, redactor=rules)

    assert _replay(pinned, redactor=rules).reproduced
    [divergence] = _replay(pinned).divergences
    assert divergence.kind == 'request'


def test_a_recorded_kernel_tool_error_ends_the_agent_like_the_original():
    document = _bash_run()
    document['agents']['Lead']['instances'][0]['tool_results'] = [
        {
            'call': [1, 1, 0],
            'raised': {'type': 'syndica.kernel.errors.ToolFatalError', 'message': 'broken'},
        }
    ]
    del document['agents']['Lead']['instances'][0]['turns'][1]
    document['expect'] = {'stop_reasons': {'Lead[0]': 'error'}}

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_recorded_context_overflow_is_not_retried_like_an_ordinary_failure():
    document = _doc(
        _lead(
            _turn(
                1,
                1,
                raises={'type': 'syndica.kernel.llm.base.ContextOverflowError', 'message': 'long'},
            ),
        ),
        host={
            'modules': ['safety', 'human-interaction', 'retry'],
            'flags': {'llm_retry_attempts': 1},
        },
        expect={'stop_reasons': {'Lead[0]': 'error'}, 'errors': {}},
    )

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_fallback_model_from_the_host_profile_serves_the_next_attempt():
    document = _retried(
        ['safety', 'human-interaction', 'retry'],
        {'llm_retry_attempts': 0, 'llm_fallbacks': 'backup'},
    )

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_frozen_roles_own_turn_limit_is_kept():
    document = _bash_run()
    document['agents']['Lead']['frozen']['max_turns'] = 1
    document['expect'] = {'stop_reasons': {'Lead[0]': 'max_turns'}}
    del document['agents']['Lead']['instances'][0]['turns'][1]

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_a_manifest_roles_own_limit_below_the_host_cap_is_kept():
    document = _capped(3)
    document['agents']['Lead']['manifest']['budget'] = {'turns': 1}

    result = _replay(document)

    assert result.reproduced, result.divergences


@pytest.mark.parametrize(
    ('call', 'recorded', 'kind'),
    [
        (_call('r1', 'Read', path='a.txt'), 'tool_results', None),
        (_call('e1', 'Edit', path='a.txt', old='a', new='b'), None, 'skip'),
    ],
    ids=['read-still-runs', 'edit-is-a-write'],
)
def test_no_write_lets_reads_through_and_stops_every_write(
    call: dict[str, Any], recorded: str | None, kind: str | None
):
    lead = _lead(
        _turn(1, tool_calls=[call]),
        _turn(2, content='done'),
        tools=('Read', 'Edit'),
        gate=[_allowed(1, 1, 0)] if recorded else [],
    )
    if recorded:
        lead['instances'][0][recorded] = [{'call': [1, 1, 0], 'output': 'contents'}]
    document = _doc(lead, host={'no_write': True})

    result = _replay(document, allow_divergence=True)

    assert [d.kind for d in result.divergences] == ([] if kind is None else [kind])


def test_an_answer_recorded_for_another_kind_of_request_is_a_divergence():
    document, _ = _asked({'kind': 'confirmation', 'answer': 'approve'})

    result = _replay(document, allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'response']
    assert 'confirmation' in divergence.detail and 'user_input' in divergence.detail


def test_an_ordered_answer_for_another_agent_is_a_divergence():
    document, _ = _asked(None)
    document['responses'] = [{'kind': 'user_input', 'agent': 'Other[0]', 'answer': 'blue'}]

    result = _replay(document, allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'response']
    assert 'Other[0]' in divergence.detail


def test_an_abort_happens_at_its_own_request_and_not_at_an_earlier_one():
    document = _two_questions()
    second = ['tool_call', _LEAD, 1, 2, 0, 'request', 0]
    document['responses'] = [r for r in document['responses'] if r['key'] != second]
    document['ids'] = [i for i in document['ids'] if i['key'] != [*second, 'reply']]
    document['host_actions'] = [{'kind': 'abort', 'key': second}]
    del document['agents']['Lead']['instances'][0]['turns'][2]
    document['expect'] = {'stop_reasons': {'Lead[0]': 'aborted'}}

    result = _replay(document)

    assert result.reproduced, result.divergences


def test_an_instance_keyed_to_another_role_is_not_bound_to_this_agent():
    document = _bash_run()
    document['agents']['Helper'] = document['agents'].pop('Lead')
    document['agents']['Lead'] = {
        'frozen': {'system_prompt': 'You lead.', 'tools': ['Bash', 'Read']},
        'instances': [{'key': ['host', 'spawn', 5], 'turns': []}],
    }

    result = _replay(document, allow_divergence=True)

    assert ('reply', '<unknown:' + _LEAD + '> 第 1 轮第 1 次调用') in [
        (d.kind, d.where) for d in result.divergences
    ]


def test_an_extra_message_the_record_has_no_id_for_is_a_divergence():
    document = _peers()
    document['ids'] = [i for i in document['ids'] if i['key'][0] != 'tool_call']

    [divergence] = _replay(document).divergences

    assert divergence.kind == 'id'


def test_a_status_that_differs_from_the_record_is_a_divergence():
    document = _bash_run()
    document['expect'] = {'statuses': {'Lead[0]': 'killed'}}

    [divergence] = _replay(document).divergences

    assert divergence.where == 'expect.statuses'


def test_dropped_events_are_a_divergence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Subscription, 'dropped', property(lambda self: 1))

    [divergence] = _replay(_bash_run()).divergences

    assert (divergence.kind, divergence.where) == ('outcome', 'events')


def _with_extra_turn(document: dict[str, Any], **instance: Any) -> dict[str, Any]:
    document['agents']['Lead']['frozen']['max_turns'] = 1
    turns = document['agents']['Lead']['instances'][0]['turns']
    turns[1] = _turn(2, tool_calls=[_call('g1', 'Bash', command='git push')])
    document['agents']['Lead']['instances'][0].update(instance)
    return document


@pytest.mark.parametrize(
    ('change', 'where'),
    [
        (
            lambda d: _with_extra_turn(d, skipped=[{'call': [1, 2, 0], 'output': 'refused'}]),
            'Lead[0] [1, 2, 0]',
        ),
        (
            lambda d: d['agents']['Lead']['instances'].append(
                {'key': ['host', 'spawn', 7], 'turns': [_turn(1, content='never')]}
            ),
            'Lead[1]',
        ),
        (
            lambda d: d.__setitem__(
                'responses',
                [
                    {
                        'key': ['tool_call', _LEAD, 1, 9, 0, 'request', 0],
                        'kind': 'user_input',
                        'answer': 'x',
                    }
                ],
            ),
            'responses[0]',
        ),
        (
            lambda d: d.__setitem__(
                'host_actions',
                [{'kind': 'abort', 'key': ['tool_call', _LEAD, 1, 9, 0, 'request', 0]}],
            ),
            "host_actions abort ['tool_call', '" + _LEAD + "', 1, 9, 0, 'request', 0]",
        ),
        (lambda d: d.__setitem__('host_actions', [{'kind': 'shutdown'}]), 'host_actions'),
    ],
    ids=['skip', 'instance', 'response', 'abort', 'host-action'],
)
def test_each_kind_of_record_the_replay_never_used_is_reported(change: Any, where: str):
    document = _bash_run()
    change(document)

    result = _replay(document, allow_divergence=True)

    assert any(d.kind == 'unconsumed' and d.where == where for d in result.divergences), (
        result.divergences
    )


def test_with_divergence_allowed_a_missing_tool_outcome_gets_a_stand_in_and_the_run_goes_on():
    document = _bash_run()
    document['agents']['Lead']['instances'][0]['tool_results'] = []

    result = _replay(document, allow_divergence=True)

    assert [(d.kind, d.where) for d in result.divergences] == [('tool', 'Lead[0] Bash [1, 1, 0]')]
    assert result.result.final_output == {'Lead[0]': 'done'}


def test_after_the_first_divergence_its_consequences_are_not_reported_again():
    document = _bash_run()
    del document['agents']['Lead']['instances'][0]['turns'][1]

    result = _replay(document)

    assert [(d.kind, d.where) for d in result.divergences] == [
        ('reply', 'Lead[0] 第 2 轮第 1 次调用')
    ]


def test_an_error_type_named_after_a_module_outside_the_kernel_keeps_only_its_name():
    document = _bash_run()
    document['agents']['Lead']['instances'][0]['tool_results'] = [
        {
            'call': [1, 1, 0],
            'raised': {'type': 'syndica.runner.replay.ConfigError', 'message': 'not a kernel one'},
        }
    ]

    result = _replay(document)

    assert result.reproduced, result.divergences
    [block] = [b for b in result.trajectories['Lead[0]'] if b.role == 'tool']
    assert 'ConfigError' in str(block.content)


def test_a_shutdown_that_never_finishes_is_reported_instead_of_waited_on(
    monkeypatch: pytest.MonkeyPatch,
):
    async def never(self: Any) -> None:
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(replay_module.Runtime, 'shutdown', never)
    started = time.monotonic()

    with pytest.raises(ContractViolation) as caught:
        _replay(_bash_run())

    assert '关闭 Runtime 没有完成' in caught.value.received
    assert time.monotonic() - started < 30


def test_after_the_root_ends_a_peer_waiting_on_a_timer_still_uses_its_records():
    document = _peers()
    failure = {'type': 'syndica.kernel.errors.LLMResponseError', 'message': 'HTTP 503'}
    document['agents']['Helper']['instances'][0]['turns'] = [
        _turn(1, 1, raises=failure),
        _turn(1, 2, content='helper done'),
    ]
    document['host'] = {
        'modules': ['safety', 'human-interaction', 'retry'],
        'flags': {'llm_retry_attempts': 1, 'llm_retry_backoff_ms': 5000},
    }

    result = _replay(document)

    assert result.reproduced, result.divergences
    assert result.result.final_output == {'Lead[0]': 'lead done', 'Helper[0]': 'helper done'}


def test_an_interrupt_after_a_last_recorded_timeout_lets_the_timeout_happen_first():
    asked, _ = _asked({'kind': 'timeout'})
    asked['agents']['Lead']['instances'][0]['turns'] = [
        _turn(1, tool_calls=[_call('q1', 'AskUser', question='which colour?', timeout=300)])
    ]
    asked['host_actions'] = [{'kind': 'interrupt'}]

    result = _replay(asked)

    assert result.reproduced, result.divergences
    assert [block.role for block in result.trajectories['Lead[0]']] == [
        'user',
        'assistant',
        'tool',
    ]


def test_an_interrupt_the_replay_reaches_without_creating_a_recorded_agent_is_a_stall():
    document = _interrupted([{'kind': 'interrupt'}])
    document['agents']['Lead']['instances'].append(
        {'key': ['host', 'spawn', 7], 'turns': [_turn(1, content='never')]}
    )

    [divergence] = _replay(document).divergences

    assert (divergence.kind, divergence.where) == ('stall', 'host_actions interrupt')
    assert 'Lead[1]' in divergence.detail


def test_with_divergence_allowed_a_call_that_did_not_run_does_not_shift_the_others_records():
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('c1', 'Bash', command='rm -rf /')]),
            _turn(2, tool_calls=[_call('c2', 'Bash', command='ls')]),
            _turn(3, content='done'),
            tool_results=[
                {'call': [1, 1, 0], 'output': 'first'},
                {'call': [1, 2, 0], 'output': 'second'},
            ],
            gate=[
                {**_BLOCKED, 'call': [1, 1, 0], 'disposition': 'allow'},
                _allowed(1, 2, 0),
            ],
        ),
        host={
            'danger': {
                'rules': [{'id': 'shell.recursive_force_remove'}],
                'table': {'shell.recursive_force_remove': 'block'},
            }
        },
    )

    result = _replay(document, allow_divergence=True)

    outputs = [str(b.content) for b in result.trajectories['Lead[0]'] if b.role == 'tool']
    assert 'first' not in outputs[0]
    assert outputs[1] == 'second'


def test_a_child_woken_again_and_cut_keeps_no_stop_reason_from_its_earlier_cycle():
    child = ['tool_call', _LEAD, 1, 1, 0, 'spawn', 0]
    document = _doc(
        _lead(
            _turn(1, tool_calls=[_call('d1', 'Delegate', agent='Helper', query='help')]),
            _turn(
                2,
                tool_calls=[
                    _call('s1', 'SendMessage', agent_id=_HELPER, content='more', trigger='pending')
                ],
            ),
            _turn(3, content='lead done'),
            gate=[_allowed(1, 1, 0), _allowed(1, 2, 0)],
        ),
        expect={'stop_reasons': {'Lead[0]': 'completed'}},
    )
    document['agents']['Helper'] = {
        'frozen': {'system_prompt': 'You help.', 'tools': ['Read']},
        'instances': [{'key': child, 'turns': [_turn(1, content='helper done')]}],
    }
    document['ids'] += [
        {'key': child, 'id': _HELPER},
        {'key': ['tool_call', _LEAD, 1, 2, 0, 'message', 0], 'id': 'e9' * 16},
    ]

    result = _replay(document)

    assert result.reproduced, result.divergences
    helper = [(b.role, b.content) for b in result.trajectories['Helper[0]']]
    assert helper == [('user', 'help'), ('assistant', 'helper done'), ('user', 'more')]


_ORIGINAL = 'ab' * 16


def _recorded(
    document: dict[str, Any], store: InMemoryTraceStore
) -> tuple[ReplayResult, TraceRecorder]:
    recorder = TraceRecorder(
        store, RuleRedactor(), level=TraceLevel.FULL, source=TraceSource(replay_of=_ORIGINAL)
    )
    result = ReplayRunner().run(
        parse_scenario_document(document, source='captured.json'), recorder=recorder
    )
    return result, recorder


def _header(store: InMemoryTraceStore, trace_id: str) -> TraceHeader:
    header = asyncio.run(store.get(trace_id))
    assert header is not None
    return header


def test_a_recorder_given_to_the_replay_records_the_replay_itself():
    store = InMemoryTraceStore()

    result, recorder = _recorded(_peers(), store)

    assert result.reproduced, result.divergences
    header = _header(store, recorder.trace_id)
    assert (header.status, header.source.replay_of) == (SpanStatus.OK, _ORIGINAL)
    spans = asyncio.run(store.spans(recorder.trace_id))
    assert {span.status for span in spans} == {SpanStatus.OK}
    agents = {span.agent_id: span for span in spans if span.kind is SpanKind.AGENT}
    assert set(agents) == {_LEAD, _HELPER}
    assert [span.attributes.get('stop_reason') for span in agents.values()] == ['completed'] * 2
    [send] = [span for span in spans if span.name == 'SendMessage']
    assert 'tool.result' in send.attributes
    assert [event.name for event in send.events] == ['message.sent']
    calls = [span for span in spans if span.kind is SpanKind.LLM_CALL]
    assert len(calls) == 3
    assert all({'llm.request', 'llm.response'} <= set(span.attributes) for span in calls)
    edges = asyncio.run(store.edges(recorder.trace_id))
    assert [edge.kind for edge in edges] == [EdgeKind.MESSAGE]


def test_a_human_answer_in_the_replay_is_recorded_on_its_wait():
    store = InMemoryTraceStore()
    document, _ = _asked({'kind': 'user_input', 'answer': 'blue'})

    result, recorder = _recorded(document, store)

    assert result.reproduced, result.divergences
    spans = asyncio.run(store.spans(recorder.trace_id))
    [wait] = [span for span in spans if span.attributes.get('wait.kind') == 'human']
    assert 'wait.answer' in wait.attributes


class _Unwritable(InMemoryTraceStore):
    def __init__(self, refusal: Exception) -> None:
        super().__init__()
        self._refusal = refusal

    async def begin(self, header: TraceHeader) -> None:
        raise self._refusal


@pytest.mark.parametrize(
    'refusal',
    [OSError('disk full'), ContractViolation(expected='e', received='disk full', next_action='n')],
)
def test_a_recorder_that_cannot_begin_leaves_the_replay_as_it_was(
    caplog: pytest.LogCaptureFixture, refusal: Exception
):
    store = _Unwritable(refusal)

    with caplog.at_level(logging.WARNING, logger='syndica.runner.replay'):
        result, _ = _recorded(_bash_run(), store)

    assert result.reproduced, result.divergences
    assert asyncio.run(store.traces()) == []
    [record] = [r for r in caplog.records if r.name == 'syndica.runner.replay']
    assert record.levelno == logging.WARNING
    assert isinstance(record.args, tuple)
    assert record.args[0] == type(refusal).__name__
    assert 'disk full' in str(record.args[1])
    assert '下一步' in record.getMessage()


class _BrokenAtTheEnd(TraceRecorder):
    """转过来的 `end_span` 一律抛；收尾时自己关掉开着的 span 照常。"""

    closing = False

    def end_span(self, span_id: str, status: SpanStatus, attributes: dict[str, Any]) -> None:
        if not self.closing:
            raise RuntimeError('recorder broke')
        super().end_span(span_id, status, attributes)

    async def finish(self, status: SpanStatus = SpanStatus.OK) -> None:
        self.closing = True
        await super().finish(status)


def test_a_recorder_that_breaks_does_not_disturb_what_the_replay_compares(
    caplog: pytest.LogCaptureFixture,
):
    recorder = _BrokenAtTheEnd(InMemoryTraceStore(), RuleRedactor(), level=TraceLevel.FULL)

    with caplog.at_level(logging.ERROR):
        result = ReplayRunner().run(
            parse_scenario_document(_bash_run(), source='captured.json'), recorder=recorder
        )

    assert result.reproduced, result.divergences


@pytest.mark.parametrize(
    ('raised', 'status'),
    [
        (ConfigError(expected='e', received='r', next_action='n'), SpanStatus.ERROR),
        (asyncio.CancelledError(), SpanStatus.CANCELLED),
    ],
)
def test_a_replay_that_does_not_finish_ends_its_recording_with_how_it_ended(
    monkeypatch: pytest.MonkeyPatch, raised: BaseException, status: SpanStatus
):
    async def broken(*args: Any, **kwargs: Any) -> ReplayResult:
        raise raised

    monkeypatch.setattr(replay_module._Replay, '_run', broken)
    store = InMemoryTraceStore()

    with pytest.raises(type(raised)):
        _recorded(_bash_run(), store)

    [header] = asyncio.run(store.traces())
    assert header.status is status


def _role_gated(role: str, host: str, recorded: str) -> dict[str, Any]:
    document = _gated(rules=[{'id': 'shell.recursive_force_remove'}], gate=_BLOCKED)
    document['version'] = 3
    document['host']['danger']['table'] = {'shell.recursive_force_remove': host}
    document['host']['danger']['roles'] = {
        'Lead': {'policy': 'table', 'table': {'shell.recursive_force_remove': role}}
    }
    document['agents']['Lead']['instances'][0]['gate'][0]['disposition'] = recorded
    return document


def test_the_role_policy_in_the_host_profile_decides_that_roles_calls():
    assert _replay(_role_gated('block', host='confirm', recorded='block')).reproduced

    result = _replay(_role_gated('confirm', host='block', recorded='block'), allow_divergence=True)

    [divergence] = [d for d in result.divergences if d.kind == 'gate']
    assert "disposition: 记录 'block'，回放 'confirm'" in divergence.detail


def test_a_role_without_its_own_policy_keeps_the_hosts_table():
    document = _role_gated('confirm', host='block', recorded='block')
    document['agents']['Helper'] = {
        'frozen': {'system_prompt': 'You help.', 'tools': []},
        'instances': [],
    }
    document['host']['danger']['roles'] = {
        'Helper': document['host']['danger']['roles'].pop('Lead')
    }
    assert _replay(document).reproduced


def test_a_manifest_roles_policy_in_the_host_profile_decides_its_calls():
    document = _role_gated('block', host='confirm', recorded='block')
    document['agents']['Lead'] = {
        'manifest': {'name': 'Lead', 'description': 'leads', 'tools': ['Bash', 'Read']},
        'instances': document['agents']['Lead']['instances'],
    }
    assert _replay(document).reproduced


def test_a_block_high_risk_role_spec_is_rebuilt_for_that_role():
    document = _role_gated('block', host='confirm', recorded='block')
    document['host']['danger']['roles'] = {'Lead': {'policy': 'block_high_risk'}}
    assert _replay(document).reproduced


def test_a_role_policy_naming_a_role_the_document_does_not_have_is_refused():
    document = _role_gated('confirm', host='block', recorded='block')
    document['host']['danger']['roles'] = {'Ghost': document['host']['danger']['roles'].pop('Lead')}
    with pytest.raises(ConfigError) as excinfo:
        _replay(document)
    assert 'host.danger.roles 的键是本文档 agents 里的角色名' in excinfo.value.expected
    assert "['Ghost']" in excinfo.value.received
    assert '两处一起改' in excinfo.value.next_action
