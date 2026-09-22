from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
from typing import Any, cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ArtifactRef, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import (
    ContextOverflowError,
    LLMConfig,
    ScriptedLLM,
    ScriptedTurn,
)
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.prefix_drift import PrefixDriftModule, PrefixDriftRecord, PrefixDriftService
from syndica.kernel.retry import RetryModule
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult


class _Echo(Tool):
    __tool_name__ = 'Echo'
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        return ToolResult(output=text)


class _Beta(Tool):
    __tool_name__ = 'Beta'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='beta')


class _Alpha(Tool):
    __tool_name__ = 'Alpha'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='alpha')


class _Gamma(Tool):
    __tool_name__ = 'Gamma'
    __tool_command_args__ = ()

    async def execute(self) -> ToolResult:
        return ToolResult(output='gamma')


class _DriftingAgent(BaseAgent):
    def __init__(
        self,
        options: AgentOptions,
        *,
        hooks: HooksCenter,
        events: EventBus,
    ) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self._prompt_calls = 0

    def get_system_prompt(self) -> str:
        self._prompt_calls += 1
        return f'系统-{self._prompt_calls}'


class _PlainLLM:
    def __init__(self, script: list[ContextBlock]) -> None:
        self._config = LLMConfig(provider='plain', model='plain-model')
        self._script = list(script)

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        del messages, kwargs
        return self._script.pop(0)


class _CountingScriptedLLM(ScriptedLLM):
    def __init__(self, script: list[ScriptedTurn]) -> None:
        super().__init__(script)
        self.prefix_calls = 0

    def cache_prefix_units(
        self,
        messages: list[ContextBlock],
        **kwargs: Any,
    ) -> tuple[bytes, ...]:
        self.prefix_calls += 1
        return super().cache_prefix_units(messages, **kwargs)


def _two_turn_factory(
    llm: Any,
    *,
    cls: type[BaseAgent] = BaseAgent,
    tools: dict[str, Tool] | None = None,
    system_prompt: str = 'stable system',
) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return cls(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools={'Echo': _Echo()} if tools is None else tools,
                system_prompt=system_prompt,
            ),
            hooks=hooks,
            events=events,
        )

    return build


def _two_turns() -> list[ScriptedTurn]:
    return [
        ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"first"}')]),
        ScriptedTurn(content='done'),
    ]


def _service(runtime: Runtime) -> PrefixDriftService:
    service = cast(Any, runtime).prefix_drift
    assert isinstance(service, PrefixDriftService)
    return service


def test_prefix_drift_public_data_shape_and_module_declaration():
    module = PrefixDriftModule()

    assert isinstance(module, RuntimeModule)
    assert module.name == 'prefix-drift'
    assert [
        (
            flag.key,
            flag.default,
            flag.choices,
            flag.minimum,
            flag.maximum,
            bool(flag.description.strip()),
        )
        for flag in module.flags
    ] == [('prefix_drift_detection', True, (), None, None, True)]
    assert tuple(module.services) == ('prefix_drift',)
    assert len(module.agent_plugins) == 1
    assert module.tool_factories == ()
    assert [field.name for field in dataclasses.fields(PrefixDriftRecord)] == [
        'agent_id',
        'unit',
        'before_digest',
        'after_digest',
        'lost_bytes',
        'estimated_tokens',
        'exact',
    ]
    signature = inspect.signature(PrefixDriftModule)
    assert tuple(signature.parameters) == ('token_counter',)
    assert signature.parameters['token_counter'].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters['token_counter'].default is None


async def _async_counter(_: bytes) -> int:
    return 1


def _counter_without_payload() -> int:
    return 1


@pytest.mark.parametrize(
    ('counter', 'anchor'),
    [
        (object(), 'callable'),
        (_async_counter, '同步'),
        (_counter_without_payload, '1 个 bytes'),
    ],
    ids=['not-callable', 'async', 'wrong-arity'],
)
def test_prefix_module_rejects_invalid_token_counters(counter: object, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        PrefixDriftModule(token_counter=counter)  # pyright: ignore[reportArgumentType]
    assert '期望:' in str(excinfo.value)
    assert '实际:' in str(excinfo.value)
    assert '下一步:' in str(excinfo.value)
    assert anchor in str(excinfo.value)


@pytest.mark.parametrize('counted', [True, -1, 1.5, '2'])
def test_token_counter_must_return_a_nonnegative_exact_integer(counted: object):
    calls: list[bytes] = []

    def invalid_counter(payload: bytes) -> int:
        calls.append(payload)
        return cast(Any, counted)

    service = PrefixDriftService(invalid_counter)
    llm = ScriptedLLM([])
    first = {
        'messages': [ContextBlock(role='system', content='one')],
        'tools': [],
    }
    second = {
        'messages': [ContextBlock(role='system', content='two')],
        'tools': [],
    }
    service._observe('agent-1', llm, first)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ContractViolation) as excinfo:
        service._observe('agent-1', llm, second)  # pyright: ignore[reportPrivateUsage]

    assert 'int' in excinfo.value.expected
    assert repr(counted) in excinfo.value.received
    assert 'token_counter' in excinfo.value.next_action
    assert service.records == ()
    with pytest.raises(ContractViolation):
        service._observe('agent-1', llm, second)  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 2


async def test_append_only_history_keeps_the_prefix_stable():
    llm = ScriptedLLM(_two_turns())
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        agent_id = await runtime.spawn(_two_turn_factory(llm), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert context.stop_reason is StopReason.COMPLETED
        assert _service(runtime).records == ()
    finally:
        await runtime.shutdown()


async def test_dynamic_system_prompt_records_the_first_changed_unit(
    caplog: pytest.LogCaptureFixture,
):
    llm = ScriptedLLM(_two_turns())
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(llm, cls=_DriftingAgent),
            query='go',
        )
        await asyncio.wait_for(runtime.wait(agent_id), 1)

        records = _service(runtime).records
        assert len(records) == 1
        record = records[0]
        first_units = llm.cache_prefix_units(llm.calls[0], **llm.call_kwargs[0])
        second_units = llm.cache_prefix_units(llm.calls[1], **llm.call_kwargs[1])
        lost = b''.join(second_units[2:])
        assert record == PrefixDriftRecord(
            agent_id=agent_id,
            unit='system',
            before_digest=hashlib.sha256(first_units[2]).hexdigest(),
            after_digest=hashlib.sha256(second_units[2]).hexdigest(),
            lost_bytes=len(lost),
            estimated_tokens=(len(lost) + 3) // 4,
            exact=True,
        )
        assert [item.levelname for item in caplog.records] == ['WARNING']
        assert agent_id in caplog.records[0].getMessage()
        assert 'system' in caplog.records[0].getMessage()
    finally:
        await runtime.shutdown()


async def test_a_third_party_llm_uses_generic_units_marked_inexact():
    llm = _PlainLLM(
        [
            ContextBlock(
                role='assistant',
                tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"first"}')],
            ),
            ContextBlock(role='assistant', content='done'),
        ]
    )
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(llm, cls=_DriftingAgent),
            query='go',
        )
        await asyncio.wait_for(runtime.wait(agent_id), 1)

        records = _service(runtime).records
        assert len(records) == 1
        assert records[0].agent_id == agent_id
        assert records[0].unit == 'system'
        assert records[0].exact is False
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    ('changed', 'expected_unit'),
    [
        ('request_args', 'request_args'),
        ('tools', 'tools'),
        ('history', 'history[0]'),
    ],
)
async def test_request_units_report_the_first_drift_location(
    changed: str,
    expected_unit: str,
):
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Beta', arguments='{}')]),
            ScriptedTurn(content='done'),
        ]
    )
    tools: dict[str, Tool] = {
        'Beta': _Beta(),
        'Alpha': _Alpha(),
        'Gamma': _Gamma(),
    }
    runtime = Runtime(modules=[PrefixDriftModule()])
    calls = 0

    async def drift(**kwargs: Any) -> HookResult:
        nonlocal calls
        calls += 1
        request = dict(kwargs['request'])
        if changed == 'request_args':
            request['temperature'] = calls
        elif changed == 'tools' and calls == 2:
            request['tools'] = list(reversed(request['tools']))
        elif changed == 'history' and calls == 2:
            messages = list(request['messages'])
            messages[1].content = 'rewritten history'
            request['messages'] = messages
        return HookResult(modified={'request': request})

    runtime.hooks.register(Junction.PRE_LLM_REQUEST, drift)
    try:
        agent_id = await runtime.spawn(_two_turn_factory(llm, tools=tools), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), 1)

        records = _service(runtime).records
        assert len(records) == 1
        assert records[0].unit == expected_unit
        assert [schema['name'] for schema in llm.call_kwargs[0]['tools']] == [
            'Beta',
            'Alpha',
            'Gamma',
        ]
    finally:
        await runtime.shutdown()


async def test_kernel_only_block_fields_do_not_change_the_provider_prefix():
    llm = ScriptedLLM(_two_turns())
    runtime = Runtime(modules=[PrefixDriftModule()])
    calls = 0

    async def change_side_channels(**kwargs: Any) -> HookResult:
        nonlocal calls
        calls += 1
        request = dict(kwargs['request'])
        if calls == 2:
            messages = list(request['messages'])
            messages[1].trust = TrustLevel.UNTRUSTED
            messages[1].extra['private'] = 'changed'
            messages[1].artifacts.append(
                ArtifactRef(
                    id='a1',
                    path='report.md',
                    kind='document',
                    mime='text/markdown',
                    version='v1',
                    producer='worker',
                )
            )
            request['messages'] = messages
        return HookResult(modified={'request': request})

    runtime.hooks.register(Junction.PRE_LLM_REQUEST, change_side_channels)
    try:
        agent_id = await runtime.spawn(_two_turn_factory(llm), query='go')
        await asyncio.wait_for(runtime.wait(agent_id), 1)
        assert _service(runtime).records == ()
    finally:
        await runtime.shutdown()


async def test_disabled_detection_does_not_compute_units_or_tokens():
    llm = _CountingScriptedLLM(_two_turns())
    token_calls: list[bytes] = []

    def count(payload: bytes) -> int:
        token_calls.append(payload)
        return 1

    runtime = Runtime(
        modules=[PrefixDriftModule(token_counter=count)],
        flags={'prefix_drift_detection': False},
    )
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(llm, cls=_DriftingAgent),
            query='go',
        )
        await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert llm.prefix_calls == 0
        assert token_calls == []
        assert _service(runtime).records == ()
    finally:
        await runtime.shutdown()


async def test_custom_token_counter_receives_the_exact_unreused_utf8_suffix():
    llm = ScriptedLLM([])
    counted: list[bytes] = []

    def count(payload: bytes) -> int:
        counted.append(payload)
        return 17

    service = PrefixDriftService(count)
    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {'messages': [ContextBlock(role='system', content='之前')], 'tools': []},
    )
    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {'messages': [ContextBlock(role='system', content='你好')], 'tools': []},
    )

    expected = '{"role":"system","content":"你好"}[]'.encode()
    record = service.records[0]
    assert counted == [expected]
    assert b'\xe4\xbd\xa0\xe5\xa5\xbd' in counted[0]
    assert b'\\u4f60' not in counted[0]
    assert record.lost_bytes == len(expected)
    assert record.estimated_tokens == 17


@pytest.mark.parametrize(
    'failure',
    [
        RuntimeError('provider failed'),
        ContextOverflowError(
            expected='请求在窗口内',
            received='超窗',
            next_action='压缩后重试',
        ),
    ],
    ids=['llm-failure', 'context-overflow'],
)
async def test_failure_exits_observe_the_final_request(failure: Exception):
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"x"}')]),
            ScriptedTurn(raises=failure),
        ]
    )
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(llm, cls=_DriftingAgent),
            query='go',
        )
        context = await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert context.stop_reason is StopReason.ERROR
        assert len(_service(runtime).records) == 1
        assert _service(runtime).records[0].unit == 'system'
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('prefix_first', [True, False], ids=['prefix-retry', 'retry-prefix'])
async def test_retry_and_prefix_modules_observe_the_same_failed_request_once(
    prefix_first: bool,
):
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"x"}')]),
            ScriptedTurn(raises=RuntimeError('retry me')),
            ScriptedTurn(content='recovered'),
        ]
    )
    prefix = PrefixDriftModule()
    retry = RetryModule({})
    modules = [prefix, retry] if prefix_first else [retry, prefix]
    runtime = Runtime(modules=modules, flags={'llm_retry_attempts': 1})
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(llm, cls=_DriftingAgent),
            query='go',
        )
        context = await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert context.stop_reason is StopReason.COMPLETED
        assert context.current_traj[-1].content == 'recovered'
        assert len(_service(runtime).records) == 1
        assert _service(runtime).records[0].unit == 'system'
    finally:
        await runtime.shutdown()


async def test_each_agent_and_fork_establishes_an_independent_first_baseline():
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        first = await runtime.spawn(
            _two_turn_factory(
                ScriptedLLM([ScriptedTurn(content='one')]),
                system_prompt='system one',
            ),
            query='one',
        )
        second = await runtime.spawn(
            _two_turn_factory(
                ScriptedLLM([ScriptedTurn(content='two')]),
                system_prompt='system two',
            ),
            query='two',
        )
        fork = await runtime.spawn(
            _two_turn_factory(
                ScriptedLLM([ScriptedTurn(content='fork')]),
                system_prompt='fork system',
            ),
            query='continue',
            fork_from=first,
        )
        await asyncio.gather(runtime.wait(first), runtime.wait(second), runtime.wait(fork))

        assert _service(runtime).records == ()
    finally:
        await runtime.shutdown()


async def test_despawn_drops_request_and_digest_baselines_but_keeps_records():
    runtime = Runtime(modules=[PrefixDriftModule()])
    try:
        agent_id = await runtime.spawn(
            _two_turn_factory(ScriptedLLM(_two_turns()), cls=_DriftingAgent),
            query='go',
        )
        await asyncio.wait_for(runtime.wait(agent_id), 1)
        service = _service(runtime)
        assert len(service.records) == 1
        assert agent_id in service._baselines  # pyright: ignore[reportPrivateUsage]
        assert agent_id in service._last_requests  # pyright: ignore[reportPrivateUsage]
        assert all(
            len(digest) == hashlib.sha256().digest_size
            for digest in service._baselines[agent_id]  # pyright: ignore[reportPrivateUsage]
        )

        await runtime.despawn(agent_id)

        assert agent_id not in service._baselines  # pyright: ignore[reportPrivateUsage]
        assert agent_id not in service._last_requests  # pyright: ignore[reportPrivateUsage]
        assert len(service.records) == 1
    finally:
        await runtime.shutdown()


def test_same_request_identity_is_observed_once_but_equal_new_requests_advance_the_baseline():
    llm = _CountingScriptedLLM([])
    service = PrefixDriftService(lambda payload: (len(payload) + 3) // 4)
    request = {
        'messages': [
            ContextBlock(role='system', content='system'),
            ContextBlock(role='user', content='go'),
        ],
        'tools': [],
    }

    service._observe('agent-1', llm, request)  # pyright: ignore[reportPrivateUsage]
    service._observe('agent-1', llm, request)  # pyright: ignore[reportPrivateUsage]
    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {
            'messages': [
                ContextBlock(role='system', content='system'),
                ContextBlock(role='user', content='go'),
            ],
            'tools': [],
        },
    )

    assert llm.prefix_calls == 2
    snapshot = service.records
    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {
            'messages': [
                ContextBlock(role='system', content='changed system'),
                ContextBlock(role='user', content='go'),
            ],
            'tools': [],
        },
    )

    assert snapshot == ()
    assert llm.prefix_calls == 3
    assert len(service.records) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        service.records[0].unit = 'changed'  # pyright: ignore[reportAttributeAccessIssue]


def test_reordering_schema_keys_is_a_tools_prefix_drift():
    llm = ScriptedLLM([])
    service = PrefixDriftService(lambda payload: (len(payload) + 3) // 4)
    first_schema = {
        'name': 'Lookup',
        'description': 'lookup',
        'parameters': {
            'required': ['query'],
            'type': 'object',
            'properties': {'query': {'type': 'string'}},
        },
    }
    reordered_schema = {
        'description': 'lookup',
        'name': 'Lookup',
        'parameters': {
            'properties': {'query': {'type': 'string'}},
            'type': 'object',
            'required': ['query'],
        },
    }
    messages = [
        ContextBlock(role='system', content='system'),
        ContextBlock(role='user', content='go'),
    ]

    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {'messages': messages, 'tools': [first_schema]},
    )
    service._observe(  # pyright: ignore[reportPrivateUsage]
        'agent-1',
        llm,
        {'messages': messages, 'tools': [reordered_schema]},
    )

    assert len(service.records) == 1
    assert service.records[0].unit == 'tools'
