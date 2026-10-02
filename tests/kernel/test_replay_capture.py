"""捕获即回放的账本（M3 T14）：recorder 写出的回放文档读得回来、记下了回放要的每一项，写不出时只少
这一份文档。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import string
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from syndica.kernel import (
    AgentOptions,
    BaseAgent,
    EventBus,
    HookResult,
    HooksCenter,
    HumanInteractionModule,
    InMemoryTraceStore,
    Junction,
    PayloadRefused,
    RedactionRule,
    RuleRedactor,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    SpanStatus,
    Tool,
    ToolCall,
    ToolResult,
    TraceLevel,
    TraceRecorder,
    parse_scenario_document,
)
from syndica.kernel.replay_capture import ReplayCapture
from syndica.kernel.trace_recorder import arguments_digest

_TIMEOUT = 5.0


def _secret(seed: int) -> str:
    rng = random.Random(seed)
    return 'sk-' + ''.join(rng.choices(string.ascii_letters + string.digits, k=40))


class _Bash(Tool):
    __tool_name__ = 'Bash'
    __tool_command_args__ = ('command',)

    def __init__(self, outputs: Mapping[str, str] | None = None) -> None:
        self._outputs = dict(outputs or {})
        self.running = asyncio.Event()

    async def execute(self, command: str) -> ToolResult:
        """跑一条命令。
        command: 命令
        """
        if command == 'explode':
            raise OSError('disk full')
        if command.endswith('hang'):
            self.running.set()
            await asyncio.get_running_loop().create_future()
        return ToolResult(output=self._outputs.get(command, f'ran {command}'), meta={'exit': 0})


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _roles(tools: list[str]) -> dict[str, dict[str, object]]:
    return {'Lead': {'system_prompt': 'You lead.', 'tools': tools}}


async def _captured(
    turns: list[ScriptedTurn],
    *,
    describe: bool = True,
    level: TraceLevel = TraceLevel.REPLAY,
    redactor: RuleRedactor | None = None,
    tools: Mapping[str, Tool] | None = None,
    prompt: str = 'go',
    system_prompt: str = 'You lead.',
    roles: Mapping[str, Mapping[str, object]] | None = None,
    host: Mapping[str, object] | None = None,
    build_twice: bool = False,
    hooks: Callable[[BaseAgent], None] | None = None,
    store: InMemoryTraceStore | None = None,
    runtime_options: Mapping[str, Any] | None = None,
) -> tuple[InMemoryTraceStore, TraceRecorder, dict[str, Any] | None]:
    store = InMemoryTraceStore() if store is None else store
    recorder = TraceRecorder(store, redactor or RuleRedactor(), level=level)
    await recorder.begin()
    llm = ScriptedLLM(turns)
    owned = dict(tools or {'Bash': _Bash()})

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=llm, tools=dict(owned), system_prompt=system_prompt)
        if build_twice:
            BaseAgent(options, hooks=hooks_center, events=events)
        agent = BaseAgent(options, hooks=hooks_center, events=events)
        if hooks is not None:
            hooks(agent)
        return agent

    runtime = Runtime(
        agents={'Lead': build}, tracer=recorder, ids=recorder.ids, **dict(runtime_options or {})
    )
    if describe:
        recorder.describe_run(
            host=host or {},
            roles=roles if roles is not None else _roles(list(owned)),
            entry={'mode': 'query', 'agent': 'Lead', 'prompt': prompt},
        )
    try:
        agent_id = await runtime.spawn(build, query=prompt)
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
    await recorder.finish(SpanStatus.OK)
    text = await store.replay(recorder.trace_id)
    return store, recorder, None if text is None else cast(dict[str, Any], json.loads(text))


def _lead(document: dict[str, Any]) -> dict[str, Any]:
    [instance] = document['agents']['Lead']['instances']
    return instance


async def test_a_described_run_writes_a_replay_document_that_reads_back():
    store, recorder, document = await _captured(
        [ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='ls')]), ScriptedTurn('done')]
    )

    assert document is not None
    parsed = parse_scenario_document(document, source='replay.json')
    instance = _lead(document)
    [(key, agent_id)] = [
        (tuple(item['key']), item['id']) for item in document['ids'] if item['key'][0] == 'host'
    ]
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is SpanStatus.OK
    assert parsed.version == 2 and parsed.origin is not None
    assert document['source']['trace_id'] == recorder.trace_id
    assert tuple(instance['key']) == key == ('host', 'spawn', 0)
    assert [(turn['turn'], turn['attempt']) for turn in instance['turns']] == [(1, 1), (2, 1)]
    first, second = instance['turns']
    assert len(first['request_digests']) >= 4 and 'request_prefix' not in first
    assert second['request_prefix'] >= 4
    full = [reply.request_digests for reply in parsed.roles['Lead'].instances[0].replies]
    assert full[1] is not None and full[0] == full[1][: second['request_prefix']]
    assert list(full[1][second['request_prefix'] :]) == second['request_digests']
    assert instance['tool_results'][0]['call'] == [1, 1, 0]
    assert instance['tool_results'][0]['output'] == 'ran ls'
    assert document['expect'] == {'stop_reasons': {'Lead[0]': 'completed'}}
    assert document['entry'] == {'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    assert len(agent_id) == 32


@pytest.mark.parametrize(
    ('options', 'why'),
    [({'describe': False}, 'not-described'), ({'level': TraceLevel.STRUCTURE}, 'structure')],
    ids=['not-described', 'structure'],
)
async def test_no_replay_document_without_a_description_or_at_structure(
    options: dict[str, Any], why: str
):
    store, recorder, document = await _captured([ScriptedTurn('done')], **options)

    header = await store.get(recorder.trace_id)
    assert document is None, why
    assert header is not None and header.status is SpanStatus.OK


async def test_an_agent_the_id_source_did_not_issue_is_named_with_its_role(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        store, recorder, document = await _captured([ScriptedTurn('done')], build_twice=True)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    header = await store.get(recorder.trace_id)
    assert document is None
    assert record.levelno == logging.WARNING
    expected, received, next_action = cast(tuple[str, str, str], record.args)
    assert 'id 源' in expected
    assert "角色 'Lead'" in received and '别处构造' in received
    assert "角色 'Lead' 的工厂" in next_action and 'Runtime(ids=' in next_action
    assert header is not None and header.status is SpanStatus.OK


async def test_a_role_the_host_did_not_describe_is_named(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        _, _, document = await _captured(
            [ScriptedTurn('done')], roles={'Other': {'system_prompt': 'x', 'tools': []}}
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert document is None
    assert "角色 'Lead' 不在其中" in cast(tuple[str, str, str], record.args)[1]


async def test_kernel_identifiers_stay_verbatim_where_a_user_rule_or_a_key_would_change_them():
    rules = RuleRedactor(rules=[RedactionRule('custom.hex', '[0-9a-f]{32}')])
    hexed = 'ab' * 16

    _, _, document = await _captured(
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Bash', command=f'echo {hexed}')]),
            ScriptedTurn('done'),
        ],
        redactor=rules,
    )

    assert document is not None
    parse_scenario_document(document, source='replay.json')
    issued = {item['id'] for item in document['ids']}
    instance = _lead(document)
    assert all(len(value) == 32 and '[REDACTED' not in value for value in issued)
    assert instance['key'] == ['host', 'spawn', 0]
    command = instance['turns'][0]['tool_calls'][0]['arguments']['command']
    assert command == 'echo [REDACTED:custom.hex]'
    assert document['source']['trace_id'] in json.dumps(document)


async def test_secrets_never_reach_the_replay_document():
    secret = _secret(7)
    redactor = RuleRedactor(secrets=[secret])

    store, recorder, document = await _captured(
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='env')]),
            ScriptedTurn(f'the token is {secret}'),
        ],
        redactor=redactor,
        tools={'Bash': _Bash({'env': f'TOKEN={secret}'})},
        system_prompt=f'You lead with {secret}.',
        roles={'Lead': {'system_prompt': f'You lead with {secret}.', 'tools': ['Bash']}},
        prompt=f'go {secret}',
    )

    text = await store.replay(recorder.trace_id)
    assert document is not None and text is not None
    for fragment in (secret, secret[3:19], secret[-16:]):
        assert fragment not in text
    assert '[REDACTED:' in document['agents']['Lead']['frozen']['system_prompt']
    assert '[REDACTED:' in document['entry']['prompt']
    assert '[REDACTED:' in _lead(document)['tool_results'][0]['output']


async def test_results_raised_errors_skips_gates_and_executed_arguments_are_recorded():
    def skip_push(agent: BaseAgent) -> None:
        async def guard(**kwargs: Any) -> HookResult:
            arguments = kwargs['args']
            if arguments.get('command') == 'git push':
                return HookResult(skip=True, result=ToolResult(output='宿主拦下了 git push'))
            return HookResult()

        agent.register_hook(Junction.PRE_TOOL_USE, guard)

    _, _, document = await _captured(
        [
            ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='ls')]),
            ScriptedTurn(tool_calls=[_call('c2', 'Bash', command='explode')]),
            ScriptedTurn(tool_calls=[_call('c3', 'Bash', command='git push')]),
            ScriptedTurn(tool_calls=[_call('c4', 'Bash', command='rm -rf /')]),
            ScriptedTurn('done'),
        ],
        hooks=skip_push,
    )

    assert document is not None
    instance = _lead(document)
    results = {tuple(item['call']): item for item in instance['tool_results']}
    assert results[(1, 1, 0)]['output'] == 'ran ls'
    assert results[(1, 1, 0)]['meta'] == {'exit': 0}
    assert results[(1, 1, 0)]['arguments_digest'] == arguments_digest(
        {'command': 'ls'}, RuleRedactor()
    )
    assert results[(1, 2, 0)]['raised'] == {'type': 'builtins.OSError', 'message': 'disk full'}
    assert set(results) == {(1, 1, 0), (1, 2, 0)}
    assert instance['skipped'] == [
        {
            'call': [1, 3, 0],
            'output': '宿主拦下了 git push',
            'meta': {},
            'trust': 'trusted',
            'artifacts': [],
        }
    ]
    gates = {tuple(gate['call']): gate for gate in instance['gate']}
    assert set(gates) == {(1, 1, 0), (1, 2, 0), (1, 4, 0)}
    assert (gates[(1, 1, 0)]['disposition'], gates[(1, 1, 0)]['outcome']) == ('allow', 'ok')
    assert gates[(1, 2, 0)]['outcome'] == 'error'
    assert gates[(1, 4, 0)]['rule_ids'] == ['shell.recursive_force_remove']
    assert (gates[(1, 4, 0)]['disposition'], gates[(1, 4, 0)]['outcome']) == ('block', 'blocked')


async def test_a_model_failure_is_recorded_with_its_type_and_attempt():
    from syndica.kernel import LLMResponseError

    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry')

    _, _, document = await _captured([ScriptedTurn(raises=failure)])

    assert document is not None
    [turn] = _lead(document)['turns']
    assert (turn['turn'], turn['attempt']) == (1, 1)
    assert turn['raises']['type'] == 'syndica.kernel.errors.LLMResponseError'
    assert 'HTTP 503' in turn['raises']['message']
    assert document['expect'] == {'stop_reasons': {'Lead[0]': 'error'}}


async def _asked(
    answer: Callable[[Runtime, Any], Any],
    *,
    expires: float = _TIMEOUT,
    level: TraceLevel = TraceLevel.REPLAY,
) -> dict[str, Any] | None:
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor(), level=level)
    await recorder.begin()
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[_call('q1', 'AskUser', question='colour?', timeout=expires)]),
            ScriptedTurn('done'),
        ]
    )

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=llm, tools={}, system_prompt='You lead.')
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(
        agents={'Lead': build},
        modules=(HumanInteractionModule(),),
        tracer=recorder,
        ids=recorder.ids,
    )
    recorder.describe_run(
        host={'modules': ['human-interaction']},
        roles=_roles([]),
        entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'},
    )
    subscription = runtime.events.subscribe()

    async def answering() -> None:
        async for event in subscription:
            if event.kind == 'user_input_requested':
                await answer(runtime, event)

    task = asyncio.create_task(answering())
    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
        subscription.close()
        await asyncio.gather(task, return_exceptions=True)
    await recorder.finish(SpanStatus.OK)
    text = await store.replay(recorder.trace_id)
    return None if text is None else cast(dict[str, Any], json.loads(text))


async def test_a_human_answer_is_recorded_under_its_request_key():
    async def reply(runtime: Runtime, event: Any) -> None:
        await runtime.send(
            event.agent_id,
            'blue',
            activate=False,
            sender='human',
            correlation_id=event.payload['request_id'],
        )

    document = await _asked(reply)

    assert document is not None
    agent_id = next(item['id'] for item in document['ids'] if item['key'] == ['host', 'spawn', 0])
    request = ['tool_call', agent_id, 1, 1, 0, 'request', 0]
    assert document['responses'] == [
        {'key': request, 'kind': 'user_input', 'answer': 'blue', 'trust': 'trusted'}
    ]
    assert {'key': [*request, 'reply'], 'id': document['ids'][-1]['id']} in document['ids']


async def test_a_question_that_expired_is_recorded_as_a_timeout():
    async def ignore(runtime: Runtime, event: Any) -> None:
        del runtime, event

    document = await _asked(ignore, expires=0.05)

    assert document is not None
    [response] = document['responses']
    assert response['kind'] == 'timeout' and response['key'][-2:] == ['request', 0]


async def test_a_question_the_host_aborted_is_a_host_action_at_its_request():
    async def abort(runtime: Runtime, event: Any) -> None:
        await runtime.abort(event.agent_id)

    document = await _asked(abort)

    assert document is not None
    assert document['responses'] == []
    [action] = document['host_actions']
    assert action['kind'] == 'abort' and action['key'][-2:] == ['request', 0]
    assert document['expect'] == {'stop_reasons': {'Lead[0]': 'aborted'}}


class _Stuck:
    """第二次调用永远不返回：原始 run 在那里被打断。"""

    def __init__(self) -> None:
        from syndica.kernel import LLMConfig

        self.config = LLMConfig(provider='test', model='stuck')
        self.calls = 0
        self.entered = asyncio.Event()

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        from syndica.kernel import ContextBlock

        self.calls += 1
        if self.calls == 1:
            return ContextBlock(role='assistant', tool_calls=[_call('c1', 'Bash', command='ls')])
        self.entered.set()
        await asyncio.get_running_loop().create_future()


async def test_an_interrupted_run_records_the_calls_still_waiting_on_the_outside_world():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = _Stuck()

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(
            name='Lead', llm=cast(Any, llm), tools={'Bash': _Bash()}, system_prompt='You lead.'
        )
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles(['Bash']), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(llm.entered.wait(), timeout=_TIMEOUT)
    recorder.interrupted()
    await runtime.shutdown()
    await recorder.finish(SpanStatus.CANCELLED)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    document = json.loads(text)
    parse_scenario_document(document, source='replay.json')
    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['llm_call', agent_id, 2, 1]]}
    ]
    assert [(turn['turn'], turn['attempt']) for turn in _lead(document)['turns']] == [(1, 1)]


async def test_a_failure_while_capturing_costs_only_the_replay_document(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def broken(self: ReplayCapture, *args: Any) -> None:
        raise RuntimeError('ledger bug')

    monkeypatch.setattr(ReplayCapture, 'exchanged', broken)

    with caplog.at_level(logging.ERROR, logger='syndica.kernel.trace_recorder'):
        store, recorder, document = await _captured(
            [ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='ls')]), ScriptedTurn('done')]
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    header = await store.get(recorder.trace_id)
    assert document is None
    assert len(errors) == 1 and 'ledger bug' in errors[0].getMessage()
    assert header is not None and header.status is SpanStatus.OK
    assert recorder.dropped == 0


async def test_a_description_that_does_not_fit_the_format_writes_no_document(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.ERROR, logger='syndica.kernel.trace_recorder'):
        store, recorder, document = await _captured(
            [ScriptedTurn('done')], host={'danger': {'rules': [{'id': 'shell.no_such_rule'}]}}
        )

    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    header = await store.get(recorder.trace_id)
    assert document is None
    assert 'describe_run' in record.getMessage()
    assert record.getMessage().count('下一步') == 1
    assert header is not None and header.status is SpanStatus.OK


async def test_a_user_rule_that_rewrites_part_of_the_document_is_named_in_a_warning(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        store, recorder, document = await _captured(
            [ScriptedTurn('done')],
            host={'danger': {'rules': [{'id': 'shell.privilege_escalation'}]}},
            redactor=RuleRedactor(rules=[RedactionRule('custom.word', 'shell')]),
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    header = await store.get(recorder.trace_id)
    assert document is None
    assert record.levelno == logging.WARNING
    rules, detail = cast(tuple[str, str], record.args)
    assert rules == 'custom.word'
    assert '[REDACTED:custom.word].privilege_escalation' in detail
    assert '收窄这条规则的 pattern' in record.getMessage()
    assert header is not None and header.status is SpanStatus.OK


async def test_an_environment_secret_that_rewrites_part_of_the_document_is_explained(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        _, _, document = await _captured(
            [ScriptedTurn('done')],
            host={'danger': {'rules': [{'id': 'shell.workspace_root_removal', 'params': {}}]}},
            redactor=RuleRedactor(secrets=('workspace',)),
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert document is None
    assert record.levelno == logging.WARNING
    assert '[REDACTED:secret.env_value]' in str(record.args)
    assert '名字像密钥的环境变量' in record.getMessage()


async def test_a_description_that_does_not_fit_stays_an_error_beside_an_unrelated_rule(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        _, _, document = await _captured(
            [ScriptedTurn('done')],
            host={'danger': {'rules': [{'id': 'shell.no_such_rule'}]}},
            redactor=RuleRedactor(rules=[RedactionRule('custom.word', 'zebra')]),
        )

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert document is None
    assert record.levelno == logging.ERROR
    assert 'describe_run' in record.getMessage()


async def test_a_store_that_refuses_the_replay_document_leaves_the_trace_without_an_outcome():
    class _Refusing(InMemoryTraceStore):
        async def put_replay(self, trace_id: str, document: Any) -> None:
            raise OSError('disk full')

    store, recorder, document = await _captured([ScriptedTurn('done')], store=_Refusing())

    header = await store.get(recorder.trace_id)
    assert document is None
    assert header is not None and header.status is None


class _RefusesReplay(InMemoryTraceStore):
    async def put_replay(self, trace_id: str, document: Any) -> None:
        raise PayloadRefused(
            expected='回放文档不超过 1 字节',
            received=f'这份 {len(str(document))} 字节',
            next_action='换一个单项上限更大的 store',
        )


async def test_a_replay_document_the_store_cannot_take_costs_only_the_document(
    caplog: pytest.LogCaptureFixture,
):
    store = _RefusesReplay()
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        _, recorder, document = await _captured([ScriptedTurn('done')], store=store)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    args = cast(tuple[object, ...], record.args)
    assert record.levelno == logging.WARNING
    assert (args[0], args[3]) == (recorder.trace_id, '换一个单项上限更大的 store')
    assert document is None
    header = await store.get(recorder.trace_id)
    assert header is not None and header.status is SpanStatus.OK
    assert recorder.complete is True


class _Sizing(InMemoryTraceStore):
    def __init__(self) -> None:
        super().__init__()
        self.payload_bytes = 0

    async def put_payload(self, trace_id: str, payload: Any) -> Any:
        self.payload_bytes += len(str(payload).encode('utf-8'))
        return await super().put_payload(trace_id, payload)


async def test_the_replay_document_and_the_payloads_grow_linearly_with_the_turns():
    async def sizes(turns: int) -> tuple[int, int]:
        script = [
            ScriptedTurn(tool_calls=[_call(f'c{index}', 'Bash', command=f'echo {index}')])
            for index in range(turns)
        ]
        store = _Sizing()
        _, recorder, _ = await _captured([*script, ScriptedTurn('done')], store=store)
        text = await store.replay(recorder.trace_id)
        assert text is not None
        return len(text), store.payload_bytes

    document_40, payloads_40 = await sizes(40)
    document_80, payloads_80 = await sizes(80)

    assert document_80 / document_40 < 2.5
    assert payloads_80 / payloads_40 < 2.5


async def test_describe_run_refuses_what_is_not_a_mapping():
    from syndica.kernel import ConfigError

    recorder = TraceRecorder(InMemoryTraceStore(), RuleRedactor())

    with pytest.raises(ConfigError) as caught:
        recorder.describe_run(host={}, roles={}, entry=cast(Any, ['query']))
    with pytest.raises(ConfigError) as role:
        recorder.describe_run(host={}, roles={'Lead': cast(Any, 'You lead.')}, entry={})

    assert 'entry' in caught.value.expected
    assert '冻结后的角色（映射）' in role.value.expected


async def test_one_stable_key_issued_twice_writes_no_document(caplog: pytest.LogCaptureFixture):
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    recorder.describe_run(host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead'})
    recorder.ids.agent_id(('host', 'spawn', 0))
    recorder.ids.agent_id(('host', 'spawn', 0))

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        await recorder.finish(SpanStatus.OK)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert await store.replay(recorder.trace_id) is None
    assert "['host', 'spawn', 0]" in cast(tuple[str, str, str], record.args)[1]


async def test_a_retried_model_call_is_recorded_under_each_attempt():
    from syndica.kernel import LLMResponseError, RetryModule

    failure = LLMResponseError(expected='a reply', received='HTTP 503', next_action='retry')

    _, _, document = await _captured(
        [ScriptedTurn(raises=failure), ScriptedTurn('done')],
        runtime_options={'modules': (RetryModule({}),), 'flags': {'llm_retry_attempts': 1}},
        host={'modules': ['retry'], 'flags': {'llm_retry_attempts': 1}},
    )

    assert document is not None
    turns = _lead(document)['turns']
    assert [(turn['turn'], turn['attempt'], 'raises' in turn) for turn in turns] == [
        (1, 1, True),
        (1, 2, False),
    ]


async def test_a_chosen_option_is_recorded_as_an_answer_to_a_question():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[
                    _call('q1', 'AskUserQuestion', question='size?', options=['small', 'large'])
                ]
            ),
            ScriptedTurn('done'),
        ]
    )

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=llm, tools={}, system_prompt='You lead.')
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(
        agents={'Lead': build},
        modules=(HumanInteractionModule(),),
        tracer=recorder,
        ids=recorder.ids,
    )
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    subscription = runtime.events.subscribe()

    async def answering() -> None:
        async for event in subscription:
            if event.kind == 'user_question_requested':
                await runtime.send(
                    event.agent_id,
                    'large',
                    activate=False,
                    sender='human',
                    correlation_id=event.payload['request_id'],
                )

    task = asyncio.create_task(answering())
    try:
        agent_id = await runtime.spawn(build, query='go')
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
        subscription.close()
        await asyncio.gather(task, return_exceptions=True)
    await recorder.finish(SpanStatus.OK)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    [response] = json.loads(text)['responses']
    assert (response['kind'], response['answer']) == ('user_question', 'large')


async def _interrupted_at_a_confirmation(
    answer: str | None,
) -> tuple[str, dict[str, Any]]:
    from syndica.kernel import Disposition, MailboxConfirmationHandler
    from syndica.kernel.danger import RuleTablePolicy

    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    bash = _Bash()
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='rm -rf hang')])])
    policy = RuleTablePolicy({'shell.recursive_force_remove': Disposition.CONFIRM})
    asked = asyncio.Event()

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(
            name='Lead',
            llm=llm,
            tools={'Bash': bash},
            system_prompt='You lead.',
            danger_policy=policy,
        )
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(
        agents={'Lead': build},
        confirmation_handler=MailboxConfirmationHandler(timeout=_TIMEOUT * 4),
        tracer=recorder,
        ids=recorder.ids,
    )
    recorder.describe_run(
        host={}, roles=_roles(['Bash']), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    subscription = runtime.events.subscribe()

    async def answering() -> None:
        async for event in subscription:
            if event.kind == 'tool_confirmation_requested':
                asked.set()
                if answer is not None:
                    await runtime.send(
                        event.agent_id,
                        answer,
                        activate=False,
                        sender='human',
                        correlation_id=event.payload['request_id'],
                    )

    task = asyncio.create_task(answering())
    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(asked.wait(), timeout=_TIMEOUT)
    if answer is not None:
        await asyncio.wait_for(bash.running.wait(), timeout=_TIMEOUT)
    recorder.interrupted()
    await runtime.shutdown()
    subscription.close()
    await asyncio.gather(task, return_exceptions=True)
    await recorder.finish(SpanStatus.CANCELLED)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    document = json.loads(text)
    parse_scenario_document(document, source='replay.json')
    return agent_id, document


async def test_an_interrupt_at_a_pending_confirmation_waits_on_the_question_not_the_tool():
    agent_id, document = await _interrupted_at_a_confirmation(None)

    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 1, 0, 'request', 0]]}
    ]


async def test_an_interrupt_while_an_approved_tool_runs_waits_on_that_tool():
    agent_id, document = await _interrupted_at_a_confirmation('approve')

    assert document['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['tool_call', agent_id, 1, 1, 0]]}
    ]
    [gate] = _lead(document)['gate']
    assert gate['disposition'] == 'confirm' and 'outcome' not in gate


async def test_an_interrupt_with_nothing_in_flight_has_no_recorded_position():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = ScriptedLLM([ScriptedTurn('done')])

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=llm, tools={}, system_prompt='You lead.')
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    await runtime.shutdown()
    await recorder.finish(SpanStatus.CANCELLED)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    assert json.loads(text)['host_actions'] == [{'kind': 'interrupt'}]


async def test_a_failure_while_assembling_the_document_costs_only_the_document(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def broken(self: ReplayCapture, *args: Any) -> Any:
        raise RuntimeError('assembly bug')

    monkeypatch.setattr(ReplayCapture, 'document', broken)

    with caplog.at_level(logging.ERROR, logger='syndica.kernel.trace_recorder'):
        store, recorder, document = await _captured([ScriptedTurn('done')])

    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    header = await store.get(recorder.trace_id)
    assert document is None
    assert 'assembly bug' in record.getMessage()
    assert header is not None and header.status is SpanStatus.OK


async def test_a_model_call_outside_any_turn_writes_no_document(caplog: pytest.LogCaptureFixture):
    from syndica.kernel import ContextBlock, SpanKind

    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    agent_id = recorder.ids.agent_id(('host', 'spawn', 0))
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    recorder.start_span('a' * 16, None, SpanKind.AGENT, 'Lead', agent_id, {'agent.role': 'Lead'})
    recorder.start_span('b' * 16, 'a' * 16, SpanKind.LLM_CALL, 'llm', agent_id, {'llm.attempt': 1})
    recorder.llm_exchange(
        'b' * 16, {'messages': []}, ContextBlock(role='assistant', content='x'), None
    )
    recorder.end_span('b' * 16, SpanStatus.OK, {})
    recorder.end_span('a' * 16, SpanStatus.OK, {'stop_reason': 'completed'})

    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        await recorder.finish(SpanStatus.OK)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert await store.replay(recorder.trace_id) is None
    assert '不在任何一轮里' in cast(tuple[str, str, str], record.args)[1]


class _ByAgent:
    """按 agent id 给出模型调用的行为：一个 Agent 一份。"""

    def __init__(self, behaviours: dict[str, Callable[[], Any]]) -> None:
        from syndica.kernel import LLMConfig

        self.config = LLMConfig(provider='test', model='by-agent')
        self.behaviours = behaviours

    async def create(self, messages: list[Any], **kwargs: Any) -> Any:
        from syndica.kernel.tracing import current_llm_call

        key = current_llm_call()
        assert key is not None
        return await self.behaviours[key[0]]()


async def test_a_call_a_kill_cut_earlier_is_not_among_the_calls_an_interrupt_cuts():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    entered = {'helper': asyncio.Event(), 'lead': asyncio.Event()}
    behaviours: dict[str, Callable[[], Any]] = {}
    llm = _ByAgent(behaviours)

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=cast(Any, llm), tools={}, system_prompt='x')
        return BaseAgent(options, hooks=hooks_center, events=events)

    def stuck(name: str) -> Callable[[], Any]:
        async def call() -> Any:
            entered[name].set()
            await asyncio.get_running_loop().create_future()

        return call

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    helper = await runtime.spawn(build, query='help')
    behaviours[helper] = stuck('helper')
    await asyncio.wait_for(entered['helper'].wait(), timeout=_TIMEOUT)
    await runtime.kill(helper)
    lead = await runtime.spawn(build, query='go')
    behaviours[lead] = stuck('lead')
    await asyncio.wait_for(entered['lead'].wait(), timeout=_TIMEOUT)
    recorder.interrupted()
    await runtime.shutdown()
    await recorder.finish(SpanStatus.CANCELLED)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    assert json.loads(text)['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['llm_call', lead, 1, 1]]}
    ]


async def _refused(
    caplog: pytest.LogCaptureFixture, **options: Any
) -> tuple[str, dict[str, Any] | None]:
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        _, _, document = await _captured([ScriptedTurn('done')], **options)
    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert record.levelno == logging.WARNING
    return cast(tuple[str, str, str], record.args)[1], document


async def test_a_rule_that_recognises_a_tool_name_writes_no_document(
    caplog: pytest.LogCaptureFixture,
):
    received, document = await _refused(
        caplog, redactor=RuleRedactor(rules=[RedactionRule('custom.tool', 'Bash')])
    )

    assert document is None
    assert "角色 'Lead' 的工具表第 0 项" in received


async def test_a_class_based_role_the_replay_cannot_rebuild_writes_no_document(
    caplog: pytest.LogCaptureFixture,
):
    roles = {'Lead': {'system_prompt': 'x', 'tools': ['Bash'], 'impl': 'acme.roles:Custom'}}

    received, document = await _refused(caplog, roles=roles)

    assert document is None
    assert "'acme.roles:Custom'" in received


async def test_two_role_names_that_redact_to_one_write_no_document(
    caplog: pytest.LogCaptureFixture,
):
    roles = {
        'Lead': {'system_prompt': 'x', 'tools': ['Bash']},
        'TeamA': {'system_prompt': 'y', 'tools': []},
        'TeamB': {'system_prompt': 'z', 'tools': []},
    }

    received, document = await _refused(
        caplog, roles=roles, redactor=RuleRedactor(rules=[RedactionRule('custom.team', 'Team[AB]')])
    )

    assert document is None
    assert '同一个' in received


async def test_a_message_the_host_sends_mid_run_writes_no_document(
    caplog: pytest.LogCaptureFixture,
):
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = ScriptedLLM([ScriptedTurn('first'), ScriptedTurn('second')])

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(name='Lead', llm=llm, tools={}, system_prompt='x')
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    await runtime.send(agent_id, 'one more thing')
    await asyncio.sleep(0)
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    await runtime.shutdown()
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        await recorder.finish(SpanStatus.OK)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert await store.replay(recorder.trace_id) is None
    assert '发了 1 条消息' in cast(tuple[str, str, str], record.args)[1]


async def test_an_abort_outside_any_request_writes_no_document(caplog: pytest.LogCaptureFixture):
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow() -> Any:
        from syndica.kernel import ContextBlock

        entered.set()
        await release.wait()
        return ContextBlock(role='assistant', tool_calls=[_call('c1', 'Bash', command='ls')])

    behaviours: dict[str, Callable[[], Any]] = {}
    llm = _ByAgent(behaviours)

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(
            name='Lead', llm=cast(Any, llm), tools={'Bash': _Bash()}, system_prompt='x'
        )
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles(['Bash']), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    agent_id = await runtime.spawn(build, query='go')
    behaviours[agent_id] = slow
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    await runtime.abort(agent_id)
    release.set()
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    await runtime.shutdown()
    with caplog.at_level(logging.WARNING, logger='syndica.kernel.trace_recorder'):
        await recorder.finish(SpanStatus.OK)

    [record] = [r for r in caplog.records if r.name == 'syndica.kernel.trace_recorder']
    assert await store.replay(recorder.trace_id) is None
    assert '在请求之外中止' in cast(tuple[str, str, str], record.args)[1]


async def test_nothing_is_kept_for_a_run_the_host_did_not_describe(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []
    for name in ('started', 'exchanged', 'returned', 'ended'):
        original = getattr(ReplayCapture, name)

        def counted(
            self: ReplayCapture, *args: Any, _name: str = name, _original: Any = original
        ) -> Any:
            calls.append(_name)
            return _original(self, *args)

        monkeypatch.setattr(ReplayCapture, name, counted)

    await _captured(
        [ScriptedTurn(tool_calls=[_call('c1', 'Bash', command='ls')]), ScriptedTurn('done')],
        describe=False,
    )

    assert calls == []


async def _synthetic(
    spans: Callable[[TraceRecorder, list[str]], None], issue: list[tuple[str | int, ...]]
) -> dict[str, Any]:
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    ids = [recorder.ids.agent_id(key) for key in issue]
    recorder.describe_run(
        host={}, roles=_roles([]), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    spans(recorder, ids)
    await recorder.finish(SpanStatus.OK)
    text = await store.replay(recorder.trace_id)
    assert text is not None
    return cast(dict[str, Any], json.loads(text))


async def test_an_agent_cut_in_its_last_cycle_has_no_stop_reason():
    from syndica.kernel import SpanKind

    def cycles(recorder: TraceRecorder, ids: list[str]) -> None:
        [agent_id] = ids
        attributes: dict[str, Any] = {'agent.role': 'Lead'}
        recorder.start_span('a' * 16, None, SpanKind.AGENT, 'Lead', agent_id, attributes)
        recorder.end_span('a' * 16, SpanStatus.OK, {'stop_reason': 'completed'})
        recorder.start_span('b' * 16, None, SpanKind.AGENT, 'Lead', agent_id, attributes)
        recorder.end_span('b' * 16, SpanStatus.CANCELLED, {})

    document = await _synthetic(cycles, [('host', 'spawn', 0)])

    assert document['expect'] == {'stop_reasons': {}}


async def test_ids_are_listed_in_key_order_whatever_order_they_were_issued_in():
    document = await _synthetic(
        lambda recorder, ids: None,
        [('host', 'spawn', 2), ('agent', 'x', 'spawn', 0), ('host', 'spawn', 0)],
    )

    keys = [item['key'] for item in document['ids']]
    assert keys == sorted(keys, key=json.dumps)
    assert keys[0][0] == 'agent'


async def test_a_second_press_does_not_move_where_the_run_was_interrupted():
    store = InMemoryTraceStore()
    recorder = TraceRecorder(store, RuleRedactor())
    await recorder.begin()
    llm = _Stuck()

    def build(hooks_center: HooksCenter, events: EventBus) -> BaseAgent:
        options = AgentOptions(
            name='Lead', llm=cast(Any, llm), tools={'Bash': _Bash()}, system_prompt='You lead.'
        )
        return BaseAgent(options, hooks=hooks_center, events=events)

    runtime = Runtime(agents={'Lead': build}, tracer=recorder, ids=recorder.ids)
    recorder.describe_run(
        host={}, roles=_roles(['Bash']), entry={'mode': 'query', 'agent': 'Lead', 'prompt': 'go'}
    )
    agent_id = await runtime.spawn(build, query='go')
    await asyncio.wait_for(llm.entered.wait(), timeout=_TIMEOUT)
    recorder.interrupted()
    await runtime.shutdown()
    recorder.interrupted()
    await recorder.finish(SpanStatus.CANCELLED)

    text = await store.replay(recorder.trace_id)
    assert text is not None
    assert json.loads(text)['host_actions'] == [
        {'kind': 'interrupt', 'waiting': [['llm_call', agent_id, 2, 1]]}
    ]


async def test_answers_are_listed_in_the_order_of_their_request_keys():
    from syndica.kernel import Message, SpanKind

    def answered(recorder: TraceRecorder, ids: list[str]) -> None:
        [agent_id] = ids
        recorder.start_span(
            'a' * 16, None, SpanKind.AGENT, 'Lead', agent_id, {'agent.role': 'Lead'}
        )
        for number, (turn, text) in enumerate([(2, 'second'), (1, 'first')]):
            span = f'{number:x}' * 16
            target = json.dumps(['tool_call', agent_id, 1, turn, 0, 'request', 0])
            recorder.start_span(
                span,
                'a' * 16,
                SpanKind.WAIT,
                'wait human',
                agent_id,
                {'wait.kind': 'human', 'wait.target': target},
            )
            recorder.wait_answer(span, Message(sender='human', receiver=agent_id, content=text))
            recorder.end_span(span, SpanStatus.OK, {'wait.outcome': 'answered'})
        recorder.end_span('a' * 16, SpanStatus.OK, {'stop_reason': 'completed'})

    document = await _synthetic(answered, [('host', 'spawn', 0)])

    assert [response['answer'] for response in document['responses']] == ['first', 'second']
