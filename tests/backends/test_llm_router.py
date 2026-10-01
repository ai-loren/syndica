"""多平台轮换：按优先级逐个试，每个平台各自指数退避。"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import Any, cast

import pytest

from syndica.backends.llm_router import BackoffPolicy, MemberHealth, ModelRouter
from syndica.kernel import LLM, ConfigError, ContractViolation, LLMConfig, LLMResponseError
from syndica.kernel.context import ContextBlock
from syndica.kernel.llm import CachePrefixProvider
from syndica.kernel.llm.base import (
    ContextOverflowError,
    _logical_cache_prefix_units,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.prefix_drift import PrefixDriftService


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Member:
    """按剧本答题的平台：剧本里的异常就抛，其余当作 assistant 回复交回。"""

    def __init__(self, name: str, script: list[BaseException | str], calls: list[str]) -> None:
        self.name = name
        self._script = list(script)
        self._calls = calls
        self._config = LLMConfig(provider='fake', model=f'{name}-model')

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        self._calls.append(self.name)
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ContextBlock(role='assistant', content=outcome)


def _down(text: str = 'HTTP 503 Service Unavailable') -> LLMResponseError:
    return LLMResponseError(expected='一条可用的回复', received=text, next_action='稍后重试')


def _router(
    members: list[tuple[str, list[BaseException | str]]],
    *,
    clock: _Clock | None = None,
    policy: BackoffPolicy | None = None,
    retry_after: Callable[[Exception], float | None] = lambda _: None,
    recoverable: Callable[[Exception], bool] = lambda _: True,
    rand: Callable[[], float] = lambda: 0.5,
) -> tuple[ModelRouter, list[str], _Clock]:
    calls: list[str] = []
    clock = clock or _Clock()
    router = ModelRouter(
        [(name, _Member(name, script, calls)) for name, script in members],
        policy=policy
        or BackoffPolicy(base=2.0, factor=2.0, maximum=300.0, jitter=0.2, max_wait=120.0),
        retry_after=retry_after,
        recoverable=recoverable,
        clock=clock,
        sleep=clock.sleep,
        rand=rand,
    )
    return router, calls, clock


def _ask() -> list[ContextBlock]:
    return [ContextBlock(role='user', content='go')]


def _retry_at(router: ModelRouter) -> dict[str, float]:
    return {health.name: health.retry_at for health in router.health()}


async def test_the_first_member_answers_and_the_rest_are_not_called() -> None:
    router, calls, _ = _router([('gemini', ['from gemini']), ('openrouter', ['unused'])])

    reply = await router.create(_ask())

    assert reply.content == 'from gemini'
    assert calls == ['gemini']


async def test_a_failing_member_hands_the_same_call_to_the_next_one() -> None:
    router, calls, clock = _router([('gemini', [_down()]), ('openrouter', ['from openrouter'])])

    reply = await router.create(_ask())

    assert reply.content == 'from openrouter'
    assert calls == ['gemini', 'openrouter']
    assert clock.sleeps == []


async def test_every_platform_failure_leaves_a_warning_with_who_how_often_how_long_and_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router, _, clock = _router(
        [('gemini', [_down('first outage'), _down('second outage')]), ('openrouter', ['a', 'b'])]
    )

    with caplog.at_level(logging.DEBUG, logger='syndica.backends.llm_router'):
        await router.create(_ask())
        clock.now = _retry_at(router)['gemini']
        await router.create(_ask())

    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.WARNING]
    assert [record.args for record in caplog.records] == [
        ('gemini', 1, 2.0, 'first outage'),
        ('gemini', 2, 4.0, 'second outage'),
    ]


async def test_the_warning_reports_the_platform_hint_when_it_outlasts_the_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router, _, _ = _router(
        [('gemini', [_down()]), ('openrouter', ['ok'])], retry_after=lambda _: 31.0
    )

    with caplog.at_level(logging.DEBUG, logger='syndica.backends.llm_router'):
        await router.create(_ask())

    assert [record.args for record in caplog.records] == [
        ('gemini', 1, 31.0, 'HTTP 503 Service Unavailable')
    ]


async def test_a_call_answered_without_failover_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router, _, _ = _router([('gemini', ['ok']), ('openrouter', ['unused'])])

    with caplog.at_level(logging.DEBUG, logger='syndica.backends.llm_router'):
        await router.create(_ask())

    assert caplog.records == []


async def test_an_error_no_platform_can_fix_is_not_logged_as_a_platform_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router, _, _ = _router(
        [('gemini', [ConfigError(expected='e', received='r', next_action='n')]), ('backup', ['x'])]
    )

    with (
        caplog.at_level(logging.DEBUG, logger='syndica.backends.llm_router'),
        pytest.raises(ConfigError),
    ):
        await router.create(_ask())

    assert caplog.records == []


async def test_members_are_tried_in_the_declared_order() -> None:
    router, calls, _ = _router([('zeta', [_down()]), ('alpha', [_down()]), ('mid', ['from mid'])])

    await router.create(_ask())

    assert calls == ['zeta', 'alpha', 'mid']


async def test_a_member_in_backoff_is_skipped_on_the_next_call() -> None:
    router, calls, _ = _router([('gemini', [_down(), 'late']), ('openrouter', ['first', 'second'])])

    await router.create(_ask())
    reply = await router.create(_ask())

    assert reply.content == 'second'
    assert calls == ['gemini', 'openrouter', 'openrouter']


async def test_a_member_is_tried_first_again_once_its_backoff_expires() -> None:
    router, calls, clock = _router(
        [('gemini', [_down(), 'gemini is back']), ('openrouter', ['first'])]
    )

    await router.create(_ask())
    clock.now = _retry_at(router)['gemini']
    reply = await router.create(_ask())

    assert reply.content == 'gemini is back'
    assert calls == ['gemini', 'openrouter', 'gemini']


async def test_backoff_grows_exponentially_and_stops_at_the_maximum() -> None:
    policy = BackoffPolicy(base=1.0, factor=10.0, maximum=50.0, jitter=0.2, max_wait=0.0)
    router, _, clock = _router(
        [('gemini', [_down(), _down(), _down(), _down()]), ('backup', ['a', 'b', 'c', 'd'])],
        policy=policy,
    )

    delays: list[float] = []
    for _ in range(4):
        started = clock.now
        await router.create(_ask())
        delays.append(_retry_at(router)['gemini'] - started)
        clock.now = _retry_at(router)['gemini']

    assert delays == [1.0, 10.0, 50.0, 50.0]


async def test_a_success_resets_the_backoff() -> None:
    router, _, clock = _router([('gemini', [_down(), 'ok', _down()]), ('backup', ['a', 'b'])])

    await router.create(_ask())
    clock.now = _retry_at(router)['gemini']
    await router.create(_ask())
    started = clock.now
    await router.create(_ask())

    assert _retry_at(router)['gemini'] - started == 2.0
    assert [health.failures for health in router.health()] == [1, 0]


async def test_a_member_that_answers_again_reports_a_clean_health_snapshot() -> None:
    router, _, clock = _router([('gemini', [_down(), 'ok']), ('backup', ['a'])])

    await router.create(_ask())
    clock.now = _retry_at(router)['gemini']
    await router.create(_ask())

    assert router.health()[0] == MemberHealth(
        name='gemini', failures=0, retry_at=-math.inf, last_error=None
    )


async def test_jitter_scales_the_backoff_within_its_band() -> None:
    low, _, _ = _router([('gemini', [_down()]), ('backup', ['a'])], rand=lambda: 0.0)
    high, _, _ = _router([('gemini', [_down()]), ('backup', ['a'])], rand=lambda: 0.999)

    await low.create(_ask())
    await high.create(_ask())

    assert _retry_at(low)['gemini'] == pytest.approx(2.0 * 0.8)
    assert _retry_at(high)['gemini'] == pytest.approx(2.0 * 1.1996)


async def test_a_retry_hint_later_than_the_backoff_wins_even_past_the_maximum() -> None:
    router, _, _ = _router(
        [('openrouter', [_down('free-models-per-day')]), ('gemini', ['ok'])],
        retry_after=lambda _: 3600.0,
    )

    await router.create(_ask())

    assert _retry_at(router)['openrouter'] == 3600.0


async def test_a_retry_hint_earlier_than_the_backoff_does_not_shorten_it() -> None:
    router, _, _ = _router(
        [('openrouter', [_down()]), ('gemini', ['ok'])], retry_after=lambda _: 0.5
    )

    await router.create(_ask())

    assert _retry_at(router)['openrouter'] == 2.0


async def test_when_every_member_is_backing_off_the_router_waits_for_the_first_to_recover() -> None:
    router, calls, clock = _router(
        [('gemini', [_down(), 'gemini recovered']), ('openrouter', [_down()])],
        retry_after=lambda exc: 30.0 if 'quota' in str(exc) else None,
    )

    reply = await router.create(_ask())

    assert reply.content == 'gemini recovered'
    assert clock.sleeps == [2.0]
    assert calls == ['gemini', 'openrouter', 'gemini']


async def test_the_router_gives_up_at_once_when_nothing_recovers_within_max_wait() -> None:
    router, calls, clock = _router(
        [
            ('gemini', [_down('HTTP 429 per-day quota')]),
            ('openrouter', [_down('HTTP 401 bad key')]),
        ],
        retry_after=lambda _: 7200.0,
    )

    with pytest.raises(LLMResponseError) as caught:
        await router.create(_ask())

    error = caught.value
    assert clock.sleeps == []
    assert calls == ['gemini', 'openrouter']
    assert '2 个平台' in error.expected
    assert '120' in error.expected
    assert error.received.index('gemini') < error.received.index('openrouter')
    assert 'HTTP 429 per-day quota' in error.received
    assert 'HTTP 401 bad key' in error.received
    assert '7200 秒' in error.received
    assert '加一个平台' in error.next_action


async def test_the_router_gives_up_once_repeated_failures_push_recovery_past_max_wait() -> None:
    policy = BackoffPolicy(base=50.0, factor=2.0, maximum=300.0, jitter=0.0, max_wait=120.0)
    router, calls, clock = _router(
        [('gemini', [_down(), _down()]), ('openrouter', [_down(), _down()])], policy=policy
    )

    with pytest.raises(LLMResponseError):
        await router.create(_ask())

    assert clock.sleeps == [50.0]
    assert calls == ['gemini', 'openrouter', 'gemini', 'openrouter']


async def test_a_member_whose_failure_waiting_cannot_fix_is_not_waited_for() -> None:
    router, calls, clock = _router(
        [
            ('gemini', [_down('HTTP 401 bad key'), _down('HTTP 401 bad key')]),
            ('openrouter', [_down('HTTP 429 per-minute'), 'openrouter recovered']),
        ],
        retry_after=lambda exc: 30.0 if '429' in str(exc) else None,
        recoverable=lambda exc: '401' not in str(exc),
    )

    reply = await router.create(_ask())

    assert reply.content == 'openrouter recovered'
    assert clock.sleeps == [30.0]
    assert calls == ['gemini', 'openrouter', 'gemini', 'openrouter']


async def test_when_no_failure_can_be_fixed_by_waiting_the_router_gives_up_at_once() -> None:
    router, calls, clock = _router(
        [('gemini', [_down('HTTP 401 bad key')]), ('openrouter', [_down('HTTP 403 forbidden')])],
        recoverable=lambda _: False,
    )

    with pytest.raises(LLMResponseError) as caught:
        await router.create(_ask())

    assert clock.sleeps == []
    assert calls == ['gemini', 'openrouter']
    assert 'HTTP 401 bad key' in caught.value.received
    assert 'HTTP 403 forbidden' in caught.value.received
    assert '401' in caught.value.next_action
    assert 'API key' in caught.value.next_action


async def test_whether_to_wait_follows_each_members_latest_failure() -> None:
    router, _, clock = _router(
        [('gemini', [_down('HTTP 401'), _down('HTTP 503'), 'gemini is back'])],
        recoverable=lambda exc: '401' not in str(exc),
    )

    with pytest.raises(LLMResponseError):
        await router.create(_ask())
    clock.now = _retry_at(router)['gemini']
    reply = await router.create(_ask())

    assert reply.content == 'gemini is back'
    assert clock.sleeps == [4.0]


async def test_waiting_is_measured_from_the_call_not_from_the_clocks_zero() -> None:
    clock = _Clock()
    clock.now = 10_000.0
    router, _, _ = _router([('gemini', [_down(), 'gemini recovered'])], clock=clock)

    reply = await router.create(_ask())

    assert reply.content == 'gemini recovered'
    assert clock.sleeps == [2.0]


async def test_the_exhausted_error_reports_each_wait_from_now() -> None:
    clock = _Clock()
    clock.now = 10_000.0
    router, _, _ = _router(
        [('gemini', [_down('HTTP 429 per-day quota')])], clock=clock, retry_after=lambda _: 7200.0
    )

    with pytest.raises(LLMResponseError) as caught:
        await router.create(_ask())

    assert '（7200 秒后' in caught.value.received


async def test_a_long_error_is_cut_short_in_health_and_in_the_exhausted_error() -> None:
    router, _, _ = _router([('gemini', [_down('x' * 1000)])], recoverable=lambda _: False)

    with pytest.raises(LLMResponseError) as caught:
        await router.create(_ask())

    [gemini] = router.health()
    assert gemini.last_error == 'x' * 300 + '…'
    assert 'x' * 301 not in caught.value.received


async def test_any_other_exception_from_a_member_is_a_platform_failure() -> None:
    router, _, _ = _router(
        [('gemini', [RuntimeError('socket closed')]), ('openrouter', ['from openrouter'])]
    )

    reply = await router.create(_ask())

    assert reply.content == 'from openrouter'
    assert router.health()[0].last_error == 'RuntimeError: socket closed'


async def test_a_single_member_is_retried_after_each_backoff() -> None:
    router, calls, clock = _router([('gemini', [_down(), _down(), 'third time'])])

    reply = await router.create(_ask())

    assert reply.content == 'third time'
    assert clock.sleeps == [2.0, 4.0]
    assert calls == ['gemini', 'gemini', 'gemini']


async def test_a_single_member_that_gives_up_names_itself_instead_of_counting() -> None:
    router, _, _ = _router([('gemini', [_down('HTTP 401 bad key')])], recoverable=lambda _: False)

    with pytest.raises(LLMResponseError) as caught:
        await router.create(_ask())

    assert 'gemini' in caught.value.expected
    assert '1 个平台' not in caught.value.expected


@pytest.mark.parametrize(
    'error',
    [
        ConfigError(expected='合法的 schema', received='含 $ref', next_action='展开 $ref'),
        ContractViolation(expected='先装门', received='没装', next_action='先 spawn'),
        ContextOverflowError(expected='放得下', received='超窗', next_action='压缩'),
    ],
)
async def test_errors_that_no_other_platform_can_fix_propagate_without_backoff(
    error: Exception,
) -> None:
    router, calls, _ = _router([('gemini', [error]), ('openrouter', ['unused'])])

    with pytest.raises(type(error)):
        await router.create(_ask())

    assert calls == ['gemini']
    assert [health.failures for health in router.health()] == [0, 0]


async def test_cancellation_is_not_recorded_as_a_platform_failure() -> None:
    entered = asyncio.Event()

    class _Hanging(_Member):
        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError('never released')

    calls: list[str] = []
    clock = _Clock()
    router = ModelRouter(
        [
            ('gemini', _Hanging('gemini', [], calls)),
            ('openrouter', _Member('openrouter', ['x'], calls)),
        ],
        clock=clock,
        sleep=clock.sleep,
    )
    task = asyncio.ensure_future(router.create(_ask()))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == []
    assert [health.failures for health in router.health()] == [0, 0]


async def test_the_reply_records_which_member_answered() -> None:
    router, _, _ = _router([('gemini', [_down()]), ('openrouter', ['from openrouter'])])

    reply = await router.create(_ask())

    assert reply.extra['llm_route'] == 'openrouter'


class _HTTPDown(LLMResponseError):
    def __init__(self, status: int) -> None:
        super().__init__(
            expected='一条可用的回复', received=f'HTTP {status}', next_action='稍后重试'
        )
        self.status_code = status


async def test_the_reply_lists_the_platforms_this_call_failed_over_from_in_order() -> None:
    router, _, _ = _router(
        [
            ('gemini', [_HTTPDown(429)]),
            ('openrouter', [RuntimeError('connection reset')]),
            ('groq', [_HTTPDown(503)]),
            ('backup', ['from backup']),
        ]
    )

    reply = await router.create(_ask())

    assert reply.extra['llm_route'] == 'backup'
    assert reply.extra['llm_failures'] == [
        {'platform': 'gemini', 'error': '_HTTPDown', 'status': 429},
        {'platform': 'openrouter', 'error': 'RuntimeError', 'status': None},
        {'platform': 'groq', 'error': '_HTTPDown', 'status': 503},
    ]


async def test_a_reply_without_failover_has_no_failure_list() -> None:
    router, _, _ = _router([('gemini', ['from gemini']), ('openrouter', ['unused'])])

    reply = await router.create(_ask())

    assert 'llm_failures' not in reply.extra


async def test_failures_while_waiting_for_a_platform_to_recover_are_all_listed() -> None:
    router, _, _ = _router([('only', [_HTTPDown(429), _HTTPDown(503), 'finally'])])

    reply = await router.create(_ask())

    assert reply.extra['llm_failures'] == [
        {'platform': 'only', 'error': '_HTTPDown', 'status': 429},
        {'platform': 'only', 'error': '_HTTPDown', 'status': 503},
    ]


async def test_a_status_that_is_not_an_integer_is_not_reported() -> None:
    odd = _HTTPDown(500)
    odd.status_code = cast(Any, True)
    router, _, _ = _router([('gemini', [odd]), ('openrouter', ['ok'])])

    reply = await router.create(_ask())

    assert reply.extra['llm_failures'] == [
        {'platform': 'gemini', 'error': '_HTTPDown', 'status': None}
    ]


class _UnitMember(_Member):
    def cache_prefix_units(self, messages: list[ContextBlock], **kwargs: Any) -> tuple[bytes, ...]:
        return (self.name.encode(), repr(sorted(kwargs)).encode(), str(len(messages)).encode())


async def test_prefix_units_come_from_the_member_that_answered_last() -> None:
    calls: list[str] = []
    router = ModelRouter(
        [
            ('gemini', _UnitMember('gemini', [_down()], calls)),
            ('openrouter', _UnitMember('openrouter', ['ok'], calls)),
        ],
        sleep=_Clock().sleep,
    )
    before = router.cache_prefix_units(_ask(), tools=[])

    await router.create(_ask())

    assert isinstance(router, CachePrefixProvider)
    assert before == (b'gemini', b"['tools']", b'1')
    assert router.cache_prefix_units(_ask(), tools=[]) == (b'openrouter', b"['tools']", b'1')


async def test_a_member_without_prefix_units_gives_none_so_drift_is_marked_inexact() -> None:
    router, _, _ = _router([('gemini', [_down()]), ('openrouter', ['ok'])])
    await router.create(_ask())
    service = PrefixDriftService(lambda unit: len(unit))
    request = {'messages': _ask(), 'temperature': 0}

    assert router.cache_prefix_units(_ask(), temperature=0) is None
    assert service._units(router, request) == (  # pyright: ignore[reportPrivateUsage]
        _logical_cache_prefix_units(router.config, _ask(), {'temperature': 0}),
        False,
    )
    assert router.config.model == 'openrouter-model'


async def test_a_router_over_members_with_units_is_exact_for_drift() -> None:
    calls: list[str] = []
    router = ModelRouter([('gemini', _UnitMember('gemini', ['ok'], calls))], sleep=_Clock().sleep)
    service = PrefixDriftService(lambda unit: len(unit))

    units, exact = service._units(router, {'messages': _ask()})  # pyright: ignore[reportPrivateUsage]

    assert (units, exact) == ((b'gemini', b'[]', b'1'), True)


async def test_each_caller_sees_the_platform_that_answered_it_not_the_last_one_overall() -> None:
    calls: list[str] = []
    router = ModelRouter(
        [
            ('a', _UnitMember('a', ['for x', _down(), 'for x again'], calls)),
            ('b', _UnitMember('b', ['for y'], calls)),
        ],
        sleep=_Clock().sleep,
        clock=_Clock(),
    )
    seen: dict[str, object] = {}
    y_done = asyncio.Event()

    async def agent_x() -> None:
        await router.create(_ask())
        await y_done.wait()
        seen['x units'] = router.cache_prefix_units(_ask())
        seen['x model'] = router.config.model

    async def agent_y() -> None:
        await asyncio.sleep(0)
        await router.create(_ask())
        seen['y model'] = router.config.model
        y_done.set()

    await asyncio.gather(agent_x(), agent_y())

    assert seen == {
        'x units': (b'a', b'[]', b'1'),
        'x model': 'a-model',
        'y model': 'b-model',
    }


async def test_a_caller_that_never_called_sees_the_last_platform_that_answered_anyone() -> None:
    router, _, _ = _router([('gemini', [_down()]), ('openrouter', ['ok'])])
    called = asyncio.Event()
    seen: list[str] = []

    async def bystander() -> None:
        await called.wait()
        seen.append(router.config.model)

    watching = asyncio.create_task(bystander())
    await router.create(_ask())
    called.set()
    await watching

    assert seen == ['openrouter-model']


async def test_failures_waiting_cannot_fix_are_listed_too() -> None:
    router, _, _ = _router(
        [('gemini', [_HTTPDown(401)]), ('openrouter', ['ok'])],
        recoverable=lambda error: False,
    )

    reply = await router.create(_ask())

    assert reply.extra['llm_failures'] == [
        {'platform': 'gemini', 'error': '_HTTPDown', 'status': 401}
    ]


@pytest.mark.parametrize('status', ['503', 503.0, True], ids=['string', 'float', 'bool'])
async def test_a_status_that_is_not_an_integer_is_reported_as_none(status: object) -> None:
    odd = _HTTPDown(500)
    odd.status_code = cast(Any, status)
    router, _, _ = _router([('gemini', [odd]), ('openrouter', ['ok'])])

    reply = await router.create(_ask())

    assert reply.extra['llm_failures'][0]['status'] is None


async def test_the_reply_is_a_copy_and_a_cached_reply_carries_no_stale_record() -> None:
    shared = ContextBlock(role='assistant', content='cached')

    class _Caching:
        config = LLMConfig(provider='fake', model='caching-model')

        async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
            return shared

    calls: list[str] = []
    router = ModelRouter(
        [('flaky', _Member('flaky', [_down(), 'unused'], calls)), ('cache', cast(Any, _Caching()))],
        sleep=_Clock().sleep,
        clock=_Clock(),
        policy=BackoffPolicy(base=1000.0, maximum=1000.0, max_wait=0),
    )

    first = await router.create(_ask())
    second = await router.create(_ask())

    assert shared.extra == {}
    assert first.extra['llm_failures'] == [
        {'platform': 'flaky', 'error': 'LLMResponseError', 'status': None}
    ]
    assert 'llm_failures' not in second.extra and second.extra['llm_route'] == 'cache'


async def test_an_outer_router_replaces_the_record_an_inner_router_left() -> None:
    calls: list[str] = []
    inner = ModelRouter(
        [
            ('inner-a', _Member('inner-a', [_down()], calls)),
            ('inner-b', _Member('inner-b', ['ok'], calls)),
        ],
        sleep=_Clock().sleep,
    )
    outer = ModelRouter([('outer-1', inner)], sleep=_Clock().sleep)

    reply = await outer.create(_ask())

    assert reply.extra['llm_route'] == 'outer-1'
    assert 'llm_failures' not in reply.extra


async def test_a_failure_is_named_by_its_class_not_its_qualified_path() -> None:
    class _LocalError(Exception):
        pass

    router, _, _ = _router([('gemini', [_LocalError('x')]), ('openrouter', ['ok'])])

    reply = await router.create(_ask())

    assert reply.extra['llm_failures'][0]['error'] == '_LocalError'


async def test_config_reports_the_member_that_answered_last() -> None:
    router, _, _ = _router([('gemini', [_down()]), ('openrouter', ['from openrouter'])])
    before = router.config

    await router.create(_ask())

    assert before.model == 'gemini-model'
    assert router.config.model == 'openrouter-model'


async def test_health_reports_every_member_with_its_last_error() -> None:
    router, _, _ = _router([('gemini', [_down('HTTP 503 overloaded')]), ('openrouter', ['ok'])])

    await router.create(_ask())

    [gemini, openrouter] = router.health()
    assert (gemini.name, gemini.failures) == ('gemini', 1)
    assert gemini.last_error is not None and 'HTTP 503 overloaded' in gemini.last_error
    assert (openrouter.name, openrouter.failures, openrouter.last_error) == ('openrouter', 0, None)


def test_the_router_is_an_llm() -> None:
    router, _, _ = _router([('gemini', ['x'])])

    assert isinstance(router, LLM)


@pytest.mark.parametrize(
    ('members', 'needle'),
    [
        ([], '至少有一个'),
        ([('gemini', 'not an llm')], 'LLM 实例'),
        ([('', 'llm')], '非空'),
        ([('gemini', 'llm'), ('gemini', 'llm')], '重复'),
    ],
)
def test_members_are_validated_with_actionable_errors(members: list[Any], needle: str) -> None:
    llm = _Member('x', [], [])
    resolved = [(name, llm if member == 'llm' else member) for name, member in members]

    with pytest.raises(ConfigError) as caught:
        ModelRouter(resolved)

    assert needle in f'{caught.value.expected}{caught.value.received}'
    assert caught.value.next_action


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('base', 0.0),
        ('base', True),
        ('maximum', -1.0),
        ('maximum', 1.0),
        ('factor', 0.5),
        ('jitter', 1.0),
        ('jitter', -0.1),
        ('max_wait', -1.0),
        ('max_wait', float('inf')),
    ],
)
def test_backoff_policy_rejects_values_that_would_break_the_ring(field: str, value: object) -> None:
    with pytest.raises(ConfigError) as caught:
        BackoffPolicy(**{field: value})  # type: ignore[arg-type]

    assert field in caught.value.expected
    assert caught.value.next_action
