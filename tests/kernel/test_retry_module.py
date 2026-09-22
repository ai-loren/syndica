from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, StopReason
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import LLMConfig, ScriptedLLM
from syndica.kernel.modules import RuntimeModule
from syndica.kernel.retry import RetryModule
from syndica.kernel.runtime import Runtime


class _RecordingLLM:
    def __init__(
        self,
        name: str,
        script: list[ContextBlock | BaseException],
        order: list[str] | None = None,
    ) -> None:
        self._config = LLMConfig(provider='test', model=name)
        self._script = list(script)
        self._order = order
        self.calls: list[tuple[list[ContextBlock], dict[str, Any]]] = []

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self.calls.append((messages, kwargs))
        if self._order is not None:
            self._order.append(self.config.model)
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _factory(llm: _RecordingLLM) -> Any:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(AgentOptions(name='Worker', llm=llm), hooks=hooks, events=events)

    return build


def _retry_agent(
    llm: _RecordingLLM,
    module: RetryModule,
    *,
    flags: dict[str, bool | int | str] | None = None,
) -> tuple[BaseAgent, HooksCenter]:
    module_hooks = HooksCenter()
    agent = BaseAgent(
        AgentOptions(name='Worker', llm=llm),
        hooks=module_hooks,
        events=EventBus(),
    )
    resolved: dict[str, bool | int | str] = {
        'llm_retry_attempts': 0,
        'llm_retry_backoff_ms': 0,
        'llm_fallbacks': '',
    }
    resolved.update(flags or {})
    plugin = module.agent_plugins[0](None, agent, resolved)  # pyright: ignore[reportArgumentType]
    plugin.register(module_hooks)
    return agent, module_hooks


def test_retry_module_declares_the_three_fixed_flags_and_constructor_shape():
    module = RetryModule({})

    assert isinstance(module, RuntimeModule)
    assert module.name == 'retry'
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
    ] == [
        ('llm_retry_attempts', 0, (), 0, None, True),
        ('llm_retry_backoff_ms', 0, (), 0, None, True),
        ('llm_fallbacks', '', (), None, None, True),
    ]
    assert len(module.agent_plugins) == 1
    assert dict(module.services) == {}
    assert module.tool_factories == ()
    assert module.on_start is None
    assert module.on_shutdown is None

    signature = inspect.signature(RetryModule)
    assert tuple(signature.parameters) == ('fallbacks', 'sleep')
    assert signature.parameters['fallbacks'].default is inspect.Parameter.empty
    assert signature.parameters['sleep'].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters['sleep'].default is not inspect.Parameter.empty
    assert ScriptedLLM([]).calls == []


def _three_parts(error: BaseException) -> None:
    text = str(error)
    assert '期望:' in text
    assert '实际:' in text
    assert '下一步:' in text


async def _sleep_without_delay() -> None:
    return None


@pytest.mark.parametrize(
    ('fallbacks', 'anchor'),
    [
        ('backup', 'Mapping'),
        ({'': _RecordingLLM('blank', [])}, 'fallback 名'),
        ({' padded': _RecordingLLM('padded', [])}, 'fallback 名'),
        ({'two,names': _RecordingLLM('comma', [])}, 'fallback 名'),
        ({3: _RecordingLLM('number', [])}, 'fallback 名'),
        ({'broken': object()}, 'LLM 实例'),
        ({'class': _RecordingLLM}, 'LLM 实例'),
    ],
    ids=[
        'not-a-mapping',
        'empty-name',
        'padded-name',
        'comma-in-name',
        'non-string-name',
        'not-an-llm',
        'llm-class',
    ],
)
def test_retry_module_rejects_unaddressable_fallback_mappings(
    fallbacks: object,
    anchor: str,
):
    with pytest.raises(ConfigError) as excinfo:
        RetryModule(fallbacks)  # pyright: ignore[reportArgumentType]
    _three_parts(excinfo.value)
    assert anchor in excinfo.value.expected
    assert excinfo.value.next_action


@pytest.mark.parametrize(
    ('sleep', 'anchor'),
    [
        (object(), 'callable'),
        (lambda _: None, 'async def'),
        (_sleep_without_delay, '1 个位置参数'),
    ],
    ids=['not-callable', 'sync', 'wrong-arity'],
)
def test_retry_module_rejects_invalid_sleep_callbacks(sleep: object, anchor: str):
    with pytest.raises(ConfigError) as excinfo:
        RetryModule({}, sleep=sleep)  # pyright: ignore[reportArgumentType]
    _three_parts(excinfo.value)
    assert anchor in str(excinfo.value)


async def test_retry_module_retries_the_original_llm_after_backoff():
    initial = RuntimeError('initial failure')
    llm = _RecordingLLM(
        'primary',
        [initial, ContextBlock(role='assistant', content='retried')],
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    runtime = Runtime(
        modules=[RetryModule({}, sleep=sleep)],
        flags={'llm_retry_attempts': 1, 'llm_retry_backoff_ms': 25},
    )
    try:
        agent_id = await runtime.spawn(_factory(llm), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert context.stop_reason is StopReason.COMPLETED
        assert context.current_traj[-1].content == 'retried'
        assert len(llm.calls) == 2
        assert sleeps == [0.025]
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    ('configured', 'anchor'),
    [
        (',alpha', '空项'),
        ('alpha,', '空项'),
        ('alpha,,beta', '空项'),
        ('   ', '空项'),
        ('alpha, alpha', '重复项'),
        ('missing', '未知名字'),
    ],
)
async def test_invalid_fallback_lists_are_rejected_before_spawn_side_effects(
    configured: str,
    anchor: str,
):
    primary = _RecordingLLM('primary', [])
    runtime = Runtime(
        modules=[
            RetryModule(
                {
                    'alpha': _RecordingLLM('alpha', []),
                    'beta': _RecordingLLM('beta', []),
                }
            )
        ]
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            await runtime.spawn(
                _factory(primary),
                flags={'llm_fallbacks': configured},
            )
        _three_parts(excinfo.value)
        assert anchor in excinfo.value.received
        assert await runtime.registry.list() == []
        assert primary.calls == []
    finally:
        await runtime.shutdown()


async def test_retries_then_fallbacks_in_declared_order_with_the_final_request_unchanged():
    order: list[str] = []
    initial = RuntimeError('initial')
    initial.add_note('existing note')
    primary = _RecordingLLM(
        'primary',
        [initial, RuntimeError('retry-1'), ValueError('retry-2')],
        order,
    )
    beta = _RecordingLLM('beta', [OSError('beta failed')], order)
    gamma = _RecordingLLM('gamma', [LookupError('gamma failed')], order)
    alpha = _RecordingLLM(
        'alpha',
        [ContextBlock(role='assistant', content='fallback succeeded')],
        order,
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        order.append('sleep')

    supplied = {'gamma': gamma, 'alpha': alpha, 'beta': beta}
    module = RetryModule(supplied, sleep=sleep)
    supplied.clear()
    supplied['intruder'] = _RecordingLLM('intruder', [], order)
    runtime = Runtime(
        modules=[module],
        flags={
            'llm_retry_attempts': 2,
            'llm_retry_backoff_ms': 40,
            'llm_fallbacks': ' beta, gamma ,alpha ',
        },
    )

    final_requests: list[dict[str, Any]] = []

    async def tune(**kwargs: Any) -> HookResult:
        request = {
            **kwargs['request'],
            'zeta': 3,
            'alpha': 1,
        }
        final_requests.append(request)
        return HookResult(modified={'request': request})

    runtime.hooks.register(Junction.PRE_LLM_REQUEST, tune)
    try:
        agent_id = await runtime.spawn(_factory(primary), query='go')
        context = await asyncio.wait_for(runtime.wait(agent_id), 1)

        assert context.stop_reason is StopReason.COMPLETED
        assert context.current_traj[-1].content == 'fallback succeeded'
        assert order == [
            'primary',
            'sleep',
            'primary',
            'sleep',
            'primary',
            'sleep',
            'beta',
            'sleep',
            'gamma',
            'sleep',
            'alpha',
        ]
        assert sleeps == [0.04] * 5
        calls = primary.calls + beta.calls + gamma.calls + alpha.calls
        first_messages = calls[0][0]
        assert [messages is first_messages for messages, _ in calls] == [True] * 6
        assert [list(kwargs) for _, kwargs in calls] == [['tools', 'zeta', 'alpha']] * 6
        final_request = final_requests[0]
        assert [kwargs['tools'] is final_request['tools'] for _, kwargs in calls] == [True] * 6
        assert initial.__notes__ == [
            'existing note',
            'RetryModule original retry 1 failed with RuntimeError: retry-1',
            'RetryModule original retry 2 failed with ValueError: retry-2',
            'RetryModule fallback beta failed with OSError: beta failed',
            'RetryModule fallback gamma failed with LookupError: gamma failed',
        ]
    finally:
        await runtime.shutdown()


async def test_a_prior_local_failure_hook_suppresses_all_retry_work():
    initial = RuntimeError('initial')
    primary = _RecordingLLM('primary', [initial])
    fallback = _RecordingLLM(
        'fallback',
        [ContextBlock(role='assistant', content='must not run')],
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    agent, _ = _retry_agent(
        primary,
        RetryModule({'fallback': fallback}, sleep=sleep),
        flags={
            'llm_retry_attempts': 3,
            'llm_fallbacks': 'fallback',
        },
    )

    async def local_recovery(**_: Any) -> HookResult:
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='local recovery')}
        )

    agent.register_hook(Junction.LLM_FAILURE, local_recovery)
    context = await agent.run('go')

    assert context.stop_reason is StopReason.COMPLETED
    assert context.current_traj[-1].content == 'local recovery'
    assert len(primary.calls) == 1
    assert fallback.calls == []
    assert sleeps == []


async def test_exhaustion_keeps_the_initial_error_and_allows_a_later_hook_to_recover():
    initial = RuntimeError('initial')
    primary = _RecordingLLM('primary', [initial, ValueError('retry failed')])
    fallback = _RecordingLLM('fallback', [OSError('fallback failed')])
    module = RetryModule({'fallback': fallback})
    agent, module_hooks = _retry_agent(
        primary,
        module,
        flags={
            'llm_retry_attempts': 1,
            'llm_fallbacks': 'fallback',
        },
    )
    seen: list[Exception] = []

    async def recover(**kwargs: Any) -> HookResult:
        seen.append(kwargs['error'])
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='later recovery')}
        )

    module_hooks.register(Junction.LLM_FAILURE, recover)
    context = await agent.run('go')

    assert context.stop_reason is StopReason.COMPLETED
    assert context.current_traj[-1].content == 'later recovery'
    assert seen == [initial]
    assert initial.__notes__ == [
        'RetryModule original retry 1 failed with ValueError: retry failed',
        'RetryModule fallback fallback failed with OSError: fallback failed',
    ]


async def test_exhaustion_without_a_later_hook_reraises_the_same_initial_error():
    initial = RuntimeError('initial')
    primary = _RecordingLLM('primary', [initial, ValueError('retry failed')])
    fallback = _RecordingLLM('fallback', [OSError('fallback failed')])
    agent, _ = _retry_agent(
        primary,
        RetryModule({'fallback': fallback}),
        flags={
            'llm_retry_attempts': 1,
            'llm_fallbacks': 'fallback',
        },
    )

    with pytest.raises(RuntimeError) as excinfo:
        await agent.run('go')

    assert excinfo.value is initial
    assert initial.__notes__ == [
        'RetryModule original retry 1 failed with ValueError: retry failed',
        'RetryModule fallback fallback failed with OSError: fallback failed',
    ]


async def test_post_response_emptying_a_retry_gets_a_fresh_budget_on_the_second_failure():
    primary = _RecordingLLM(
        'primary',
        [
            RuntimeError('initial'),
            ContextBlock(role='assistant', content='temporary'),
            ContextBlock(role='assistant', content='recovered'),
        ],
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    agent, module_hooks = _retry_agent(
        primary,
        RetryModule({}, sleep=sleep),
        flags={'llm_retry_attempts': 1},
    )
    failures: list[Exception] = []

    async def empty_first_retry(**kwargs: Any) -> HookResult:
        response = kwargs['response']
        if response.content == 'temporary':
            return HookResult(modified={'response': ContextBlock(role='assistant')})
        return HookResult()

    async def observe_failure(**kwargs: Any) -> HookResult:
        failures.append(kwargs['error'])
        return HookResult()

    agent.register_hook(Junction.POST_LLM_RESPONSE, empty_first_retry)
    module_hooks.register(Junction.LLM_FAILURE, observe_failure)
    context = await agent.run('go')

    assert context.stop_reason is StopReason.COMPLETED
    assert context.current_traj[-1].content == 'recovered'
    assert [type(error).__name__ for error in failures] == [
        'RuntimeError',
        'LLMResponseError',
    ]
    assert len(primary.calls) == 3
    assert sleeps == [0.0, 0.0]


@pytest.mark.parametrize('cancel_at', ['sleep', 'retry', 'fallback'])
async def test_cancellation_propagates_from_every_retry_stage(cancel_at: str):
    cancellation = asyncio.CancelledError(cancel_at)
    primary_script: list[ContextBlock | BaseException] = [RuntimeError('initial')]
    fallback_script: list[ContextBlock | BaseException] = [
        ContextBlock(role='assistant', content='unused')
    ]
    attempts = 1
    fallback_names = ''

    async def sleep(_: float) -> None:
        if cancel_at == 'sleep':
            raise cancellation

    if cancel_at == 'retry':
        primary_script.append(cancellation)
    elif cancel_at == 'fallback':
        attempts = 0
        fallback_names = 'backup'
        fallback_script = [cancellation]

    primary = _RecordingLLM('primary', primary_script)
    backup = _RecordingLLM('backup', fallback_script)
    agent, _ = _retry_agent(
        primary,
        RetryModule({'backup': backup}, sleep=sleep),
        flags={
            'llm_retry_attempts': attempts,
            'llm_fallbacks': fallback_names,
        },
    )

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await agent.run('go')
    assert excinfo.value is cancellation


async def test_two_agents_interleave_without_sharing_retry_counters():
    first = _RecordingLLM(
        'first',
        [RuntimeError('first initial'), ContextBlock(role='assistant', content='first done')],
    )
    second = _RecordingLLM(
        'second',
        [
            RuntimeError('second initial'),
            RuntimeError('second retry'),
            ContextBlock(role='assistant', content='second done'),
        ],
    )

    async def yield_once(_: float) -> None:
        await asyncio.sleep(0)

    runtime = Runtime(modules=[RetryModule({}, sleep=yield_once)])
    try:
        first_id = await runtime.spawn(
            _factory(first),
            query='one',
            flags={'llm_retry_attempts': 1},
        )
        second_id = await runtime.spawn(
            _factory(second),
            query='two',
            flags={'llm_retry_attempts': 2},
        )
        first_context, second_context = await asyncio.gather(
            runtime.wait(first_id),
            runtime.wait(second_id),
        )

        assert first_context.current_traj[-1].content == 'first done'
        assert second_context.current_traj[-1].content == 'second done'
        assert len(first.calls) == 2
        assert len(second.calls) == 3
    finally:
        await runtime.shutdown()
