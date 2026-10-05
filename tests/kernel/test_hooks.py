import asyncio
import copy
import dataclasses
import logging
import pickle
from collections.abc import Callable
from typing import cast

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks import (
    INTERCEPTABLE,
    OBSERVABLE,
    HookCallback,
    HookResult,
    HooksCenter,
    Junction,
    JunctionKind,
    ReadOnlyKwarg,
)
from syndica.kernel.hooks import center as center_module

# 并发用例回归时应当变红，而不是把 CI 挂住。
_TIMEOUT = 1.0


def test_eighteen_junctions_exist():
    """取值撞车会让成员静默塌成别名；切点名计入公开 API。"""
    assert {junction.name: junction.value for junction in Junction} == {
        'RUNTIME_SHUTDOWN': 'runtime_shutdown',
        'AGENT_SPAWN': 'agent_spawn',
        'AGENT_DESPAWN': 'agent_despawn',
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'MESSAGE_INJECTED': 'message_injected',
        'PRE_TOOL_USE': 'pre_tool_use',
        'POST_TOOL_USE': 'post_tool_use',
        'TOOL_CONFIRMATION_REQUIRED': 'tool_confirmation_required',
        'PRE_LLM_REQUEST': 'pre_llm_request',
        'POST_LLM_RESPONSE': 'post_llm_response',
        'CONTEXT_OVERFLOW': 'context_overflow',
        'LLM_FAILURE': 'llm_failure',
        'ARTIFACT_REGISTERED': 'artifact_registered',
        'COMPLETION_PROPOSED': 'completion_proposed',
    }


def test_each_junction_is_classified_as_declared():
    """分类决定语义，与切点名同属公开 API：挪动一个切点就改了它的执行方式。"""
    assert INTERCEPTABLE == {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
        Junction.COMPLETION_PROPOSED,
    }
    assert OBSERVABLE == {
        Junction.RUNTIME_SHUTDOWN,
        Junction.AGENT_SPAWN,
        Junction.AGENT_DESPAWN,
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
        Junction.TOOL_CONFIRMATION_REQUIRED,
        Junction.ARTIFACT_REGISTERED,
    }


def test_the_two_classifications_partition_the_junctions():
    overlap = INTERCEPTABLE & OBSERVABLE
    assert not overlap, (
        '期望两个分类互不相交。'
        f' 实际两边都有: {sorted(junction.value for junction in overlap)}。'
        ' 一个切点要么可拦截、要么可观察，只能二选一。'
    )
    unclassified = set(Junction) - INTERCEPTABLE - OBSERVABLE
    assert not unclassified, (
        '期望两个分类的并集恰好是 Junction 的全部成员。'
        f' 实际未归类: {sorted(junction.value for junction in unclassified)}。'
        ' 把新切点放进 INTERCEPTABLE（串行、可改 kwargs、异常上抛）或 OBSERVABLE'
        '（并行、丢弃返回值、吞异常），别让它靠遗漏拿到默认语义。'
    )


def test_hook_result_defaults_to_no_opinion():
    result = HookResult()
    assert (result.skip, result.modified, result.result) == (False, {}, None)


def test_hook_result_modified_is_not_shared_between_instances():
    first = HookResult()
    second = HookResult()
    first.modified['request'] = 'rewritten'
    assert second.modified == {}


def test_get_reads_modified_and_otherwise_returns_the_default():
    result = HookResult(modified={'request': 'rewritten'})
    assert result.get('request', 'original') == 'rewritten'
    assert result.get('tool_call', 'unchanged') == 'unchanged'
    assert result.get('tool_call') is None


async def test_observable_hooks_all_run():
    center = HooksCenter()
    calls: list[str] = []

    async def first(**_: object) -> None:
        calls.append('first')

    async def second(**_: object) -> None:
        calls.append('second')

    center.register(Junction.TURN_START, first)
    center.register(Junction.TURN_START, second)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['first', 'second']


async def test_observable_hooks_run_concurrently():
    """一个慢的可观察 hook 不得把同一切点上的其他 hook 串起来等；串行执行会卡死在栅栏上。"""
    center = HooksCenter()
    both_started = asyncio.Barrier(2)

    async def wait_for_the_other(**_: object) -> None:
        await both_started.wait()

    center.register(Junction.TURN_START, wait_for_the_other)
    center.register(Junction.TURN_START, wait_for_the_other)
    await asyncio.wait_for(center._dispatch(Junction.TURN_START, turn=1), timeout=_TIMEOUT)


def _reported_callback(record: logging.LogRecord) -> object:
    """诊断记录里被点名的那个回调。"""
    return cast('tuple[object, ...]', record.args)[1]


async def test_an_observable_hooks_returned_result_is_discarded_but_leaves_a_diagnostic(
    caplog: pytest.LogCaptureFixture,
):
    """回调类型对所有切点都是 `HookResult | None`，在可观察切点上返回它不报错也不生效——typing
    看不见，`register` 时也还不知道回调会返回什么，所以这条记录是这次丢弃留下的全部痕迹。"""
    center = HooksCenter()
    calls: list[str] = []

    async def opinionated(**_: object) -> HookResult:
        calls.append('opinionated')
        return HookResult(skip=True, modified={'turn': 99}, result='substitute')

    async def after(**_: object) -> None:
        calls.append('after')

    center.register(Junction.TURN_START, opinionated)
    center.register(Junction.TURN_START, after)
    result = await center._dispatch(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['after', 'opinionated']
    assert (result.skip, result.modified, result.result) == (False, {}, None)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.args == (
        'turn_start',
        (
            'test_an_observable_hooks_returned_result_is_discarded_but_leaves_a_diagnostic'
            '.<locals>.opinionated'
        ),
        "HookResult(skip=True, modified=['turn'], result='substitute')",
    )


async def test_an_observer_that_answers_the_confirmation_junction_is_told_it_was_discarded(
    caplog: pytest.LogCaptureFixture,
):
    """`tool_confirmation_required` 是第一个「hook 作者会真心想回答」的可观察切点：它报告的是
    「这次调用等人确认」，挂上去的人最自然的写法就是返回一个批准。那个批准照旧被整个丢弃。"""
    center = HooksCenter()

    async def approve(**_: object) -> HookResult:
        return HookResult(skip=True, result='approved')

    center.register(Junction.TOOL_CONFIRMATION_REQUIRED, approve)
    await center._dispatch(Junction.TOOL_CONFIRMATION_REQUIRED, tool_call=None)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.args == (
        'tool_confirmation_required',
        (
            'test_an_observer_that_answers_the_confirmation_junction_is_told_it_was_discarded'
            '.<locals>.approve'
        ),
        "HookResult(skip=True, modified=[], result='approved')",
    )


@pytest.mark.parametrize(
    'outcome',
    [
        HookResult(skip=True),
        HookResult(modified={'turn': 99}),
        HookResult(result='substitute'),
        HookResult(result=False),
        HookResult(result=0),
        HookResult(result=''),
        HookResult(result=[]),
        'not a HookResult at all',
    ],
    ids=[
        'skip',
        'modified',
        'result',
        'result-false',
        'result-zero',
        'result-empty-string',
        'result-empty-list',
        'off-contract',
    ],
)
async def test_every_slot_a_discarded_return_could_have_used_is_reported(
    outcome: object, caplog: pytest.LogCaptureFixture
):
    """三个槽位各自单独出现时都要报：只认其中一个，另外两种意图就仍然静默丢失。

    `result` 按取值族取到 falsy-but-not-None：`tool_confirmation_required` 上「否决这次确认」最自然
    的写法就是 `HookResult(result=False)`，判据按真值取就把整个否决族静默放过。"""
    center = HooksCenter()

    async def opinionated(**_: object) -> HookResult | None:
        # 标注是契约，`off-contract` 这一档刻意违反它：要挡的正是运行期真会出现的取值。
        return cast('HookResult | None', outcome)

    center.register(Junction.TURN_START, opinionated)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert [record.levelno for record in caplog.records] == [logging.ERROR]


@pytest.mark.parametrize('outcome', [None, HookResult()], ids=['none', 'empty-hook-result'])
async def test_an_observable_hook_that_discards_nothing_leaves_no_diagnostic(
    outcome: HookResult | None, caplog: pytest.LogCaptureFixture
):
    """记录说的是一次**丢失**。`HookCallback` 对两类切点是同一个类型，`return HookResult()` 因此
    是类型正确的写法，而空结论在可拦截切点上也什么都不做——报它就是让唯一的那份证据说谎。"""
    center = HooksCenter()

    async def observer(**_: object) -> HookResult | None:
        return outcome

    center.register(Junction.TURN_START, observer)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert caplog.records == []


async def test_an_interceptable_hooks_opinion_is_never_reported_as_discarded(
    caplog: pytest.LogCaptureFixture,
):
    """可拦截切点读这些槽位，报它等于把「用对了」说成一次丢失。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'args': {}}, result='substitute')

    center.register(Junction.PRE_TOOL_USE, rewrite)
    await center._dispatch(Junction.PRE_TOOL_USE, args={})
    assert caplog.records == []


async def test_the_discarded_return_diagnostic_names_every_junction_the_hook_could_move_to(
    caplog: pytest.LogCaptureFixture,
):
    """这条记录是这次丢弃留下的全部痕迹，而它唯一可执行的内容是「改挂到哪去」。可拦截切点少列一个，
    作者就以为那里同样读不到自己的返回值；三件套的标签少一个，读者就得自己找哪段是下一步。"""
    center = HooksCenter()

    async def opinionated(**_: object) -> HookResult:
        return HookResult(skip=True)

    center.register(Junction.TURN_START, opinionated)
    await center._dispatch(Junction.TURN_START, turn=1)
    (record,) = caplog.records
    message = record.getMessage()
    assert [junction.value for junction in INTERCEPTABLE if junction.value not in message] == []
    assert '返回 None' in message
    assert [label for label in ('期望:', '实际:', '下一步:') if label not in message] == []


_INTENT_PROBES: list[object] = [
    None,
    HookResult(),
    HookResult(skip=True),
    HookResult(modified={'turn': 99}),
    HookResult(result='substitute'),
    HookResult(result=False),
    HookResult(result=0),
    HookResult(history={'turn': [99]}),
    HookResult(skip=True, modified={'turn': 99}, result='substitute'),
    'not a HookResult at all',
]


@pytest.mark.parametrize(
    'outcome',
    _INTENT_PROBES,
    ids=[
        'none',
        'empty',
        'skip',
        'modified',
        'result',
        'result-false',
        'result-zero',
        'history-only',
        'every-slot',
        'off-contract',
    ],
)
async def test_a_discarded_return_is_reported_exactly_when_the_serial_chain_would_read_it(
    outcome: object,
):
    """判据的派生源是 `_trigger_serial` 的读取集，不是 `HookResult` 的字段清单：加第五个槽位时改的
    是那条链，而它一旦开始读，这条记录就得跟着报。两侧逐取值相等，任一侧单独漂移即红。

    `history` 那一档是这条派生源与字段清单的分界：它是字段，而链忽略 hook 自填的那一份、按
    `modified` 自己累积，按字段清单派生就会为它报一次没发生的丢失。"""
    center = HooksCenter()

    async def cb(**_: object) -> HookResult | None:
        # 标注是契约，`off-contract` 那一档刻意违反它：要挡的正是运行期真会出现的取值。
        return cast('HookResult | None', outcome)

    center.register(Junction.PRE_TOOL_USE, cb)
    try:
        merged = await center._dispatch(Junction.PRE_TOOL_USE, args={})
    except Exception:
        # 链读它的槽位时抛出，本身就是一次可观察效果。
        reaches_the_chain = True
    else:
        reaches_the_chain = merged != HookResult()
    assert center_module._carries_intent(outcome) is reaches_the_chain


async def test_each_discarding_observable_hook_gets_its_own_diagnostic(
    caplog: pytest.LogCaptureFixture,
):
    """记录点名的是**哪个回调**，所以它必须逐个配对，而不是整批报一条。"""
    center = HooksCenter()

    async def first(**_: object) -> HookResult:
        return HookResult(skip=True)

    async def quiet(**_: object) -> None:
        return None

    async def second(**_: object) -> HookResult:
        return HookResult(modified={'turn': 99})

    for cb in (first, quiet, second):
        center.register(Junction.TURN_START, cb)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert [_reported_callback(record) for record in caplog.records] == [
        'test_each_discarding_observable_hook_gets_its_own_diagnostic.<locals>.first',
        'test_each_discarding_observable_hook_gets_its_own_diagnostic.<locals>.second',
    ]


async def test_an_observable_hook_that_raises_does_not_break_trigger(
    caplog: pytest.LogCaptureFixture,
):
    """可观测性不得拖垮主流程：异常只记日志，同切点的其余 hook 照跑，trigger 正常返回。"""
    center = HooksCenter()
    calls: list[str] = []
    boom = RuntimeError('observable hook blew up')

    async def explodes(**_: object) -> None:
        calls.append('explodes')
        raise boom

    async def survivor(**_: object) -> None:
        calls.append('survivor')

    center.register(Junction.TURN_START, explodes)
    center.register(Junction.TURN_START, survivor)
    result = await center._dispatch(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['explodes', 'survivor']
    assert (result.skip, result.modified, result.result) == (False, {}, None)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is boom


async def test_a_sibling_that_awaits_is_waited_for_after_an_observable_hook_raises(
    caplog: pytest.LogCaptureFixture,
):
    """真实的可观察 hook（落日志、发指标）必然让出控制权，被丢下的兄弟迟早也会跑完；
    因此断言必须取 trigger 返回那一刻的快照，否则测的是事件循环的调度顺序而非等待语义。"""
    center = HooksCenter()
    calls: list[str] = []
    finished_when_trigger_returned: list[str] = []
    boom = RuntimeError('observable hook blew up')
    exploded = asyncio.Event()
    unblocked = asyncio.Event()

    async def explodes(**_: object) -> None:
        calls.append('explodes')
        exploded.set()
        raise boom

    async def survivor(**_: object) -> None:
        await unblocked.wait()
        calls.append('survivor')

    async def trigger_and_snapshot() -> None:
        await center._dispatch(Junction.TURN_START, turn=1)
        finished_when_trigger_returned.extend(calls)

    center.register(Junction.TURN_START, explodes)
    center.register(Junction.TURN_START, survivor)
    triggering = asyncio.create_task(trigger_and_snapshot())
    await asyncio.wait_for(exploded.wait(), timeout=_TIMEOUT)
    unblocked.set()
    await asyncio.wait_for(triggering, timeout=_TIMEOUT)
    assert finished_when_trigger_returned == ['explodes', 'survivor']
    (record,) = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[1] is boom


async def test_every_failing_observable_hook_is_logged(caplog: pytest.LogCaptureFixture):
    center = HooksCenter()
    first_boom = RuntimeError('first observable hook blew up')
    second_boom = RuntimeError('second observable hook blew up')

    async def explodes_first(**_: object) -> None:
        raise first_boom

    async def explodes_second(**_: object) -> None:
        raise second_boom

    center.register(Junction.TURN_START, explodes_first)
    center.register(Junction.TURN_START, explodes_second)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == [
        first_boom,
        second_boom,
    ]


async def test_interceptable_hook_can_modify_kwargs():
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center._dispatch(Junction.PRE_LLM_REQUEST, request='original')
    assert result.get('request', 'original') == 'rewritten'


async def test_modified_kwargs_reach_the_next_hook_alongside_the_untouched_ones():
    """中间件语义的全部：只传第一个 hook 的改动、或只传改动本身，链条都无法组合。"""
    center = HooksCenter()
    seen: list[dict[str, object]] = []

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'request': 'from-first'})

    async def second(**kwargs: object) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult()

    center.register(Junction.PRE_LLM_REQUEST, first)
    center.register(Junction.PRE_LLM_REQUEST, second)
    await center._dispatch(Junction.PRE_LLM_REQUEST, request='original', model='gpt')
    assert seen == [{'request': 'from-first', 'model': 'gpt'}]


async def test_modified_reports_only_the_changed_keys():
    """调用方靠 `modified` 分辨哪些参数被改过；回整份 kwargs 就分辨不出来了。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center._dispatch(Junction.PRE_LLM_REQUEST, request='original', model='gpt')
    assert result.modified == {'request': 'rewritten'}


async def test_every_hooks_modification_is_carried_out_of_the_chain():
    """链条存在的理由就是组合；调用方只拿到最后一个 hook 的改动，前面的中间件等于没跑。"""
    center = HooksCenter()

    async def redact(**_: object) -> HookResult:
        return HookResult(modified={'request': 'redacted'})

    async def enforce_budget(**_: object) -> HookResult:
        return HookResult(modified={'max_tokens': 100})

    async def audit(**_: object) -> HookResult:
        return HookResult(modified={'audited': True})

    for cb in (redact, enforce_budget, audit):
        center.register(Junction.PRE_LLM_REQUEST, cb)
    result = await center._dispatch(Junction.PRE_LLM_REQUEST, request='original')
    assert result.modified == {'request': 'redacted', 'max_tokens': 100, 'audited': True}


async def test_the_returned_modified_holds_the_values_the_chain_actually_applied():
    """`modified` 与流进下一个 hook 的 kwargs 是同一份结论；两者分叉，调用方就按一个没人执行过的
    版本回放。同一个键被改两次、被改成 None，都属于这条链正常的写法。"""
    center = HooksCenter()
    seen: list[dict[str, object]] = []

    async def redact(**_: object) -> HookResult:
        return HookResult(modified={'request': 'redacted'})

    async def truncate(**_: object) -> HookResult:
        return HookResult(modified={'request': 'truncated', 'tool_call': None})

    async def reader(**kwargs: object) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult()

    for cb in (redact, truncate, reader):
        center.register(Junction.PRE_TOOL_USE, cb)
    result = await center._dispatch(Junction.PRE_TOOL_USE, request='original', tool_call='shell')
    assert seen == [{'request': 'truncated', 'tool_call': None}]
    assert result.modified == {'request': 'truncated', 'tool_call': None}


async def test_skip_carries_out_the_modifications_made_before_it_and_its_own():
    center = HooksCenter()

    async def redact(**_: object) -> HookResult:
        return HookResult(modified={'request': 'redacted'})

    async def deny(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'audited': True}, result='denied')

    center.register(Junction.PRE_TOOL_USE, redact)
    center.register(Junction.PRE_TOOL_USE, deny)
    result = await center._dispatch(Junction.PRE_TOOL_USE, request='original')
    assert result.skip is True
    assert result.result == 'denied'
    assert result.modified == {'request': 'redacted', 'audited': True}


async def test_a_hook_returning_none_at_an_interceptable_junction_changes_nothing():
    """可拦截切点上也挂得住纯观察者：返回 None 即「无意见」。"""
    center = HooksCenter()
    seen: list[object] = []

    async def observer(**_: object) -> None:
        return None

    async def reader(**kwargs: object) -> HookResult:
        seen.append(kwargs['request'])
        return HookResult()

    center.register(Junction.PRE_LLM_REQUEST, observer)
    center.register(Junction.PRE_LLM_REQUEST, reader)
    result = await center._dispatch(Junction.PRE_LLM_REQUEST, request='original')
    assert seen == ['original']
    assert (result.skip, result.modified, result.result) == (False, {}, None)


async def test_skip_stops_subsequent_interceptable_hooks():
    center = HooksCenter()
    calls: list[str] = []

    async def blocker(**_: object) -> HookResult:
        calls.append('blocker')
        return HookResult(skip=True, result='substitute')

    async def never(**_: object) -> HookResult:
        calls.append('never')
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, blocker)
    center.register(Junction.PRE_TOOL_USE, never)
    result = await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert calls == ['blocker']
    assert result.skip is True
    assert result.result == 'substitute'


async def test_an_interceptable_hook_that_raises_propagates():
    """可拦截 hook 是策略；跳过一个失败的策略检查等于放行未经检查的操作。"""
    center = HooksCenter()
    calls: list[str] = []
    boom = RuntimeError('policy check blew up')

    async def explodes(**_: object) -> HookResult:
        calls.append('explodes')
        raise boom

    async def never(**_: object) -> HookResult:
        calls.append('never')
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, explodes)
    center.register(Junction.PRE_TOOL_USE, never)
    with pytest.raises(RuntimeError) as excinfo:
        await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert excinfo.value is boom
    assert calls == ['explodes']


async def test_trigger_with_no_hooks_returns_empty_result():
    center = HooksCenter()
    result = await center._dispatch(Junction.TURN_END, turn=1)
    assert result.skip is False
    assert result.modified == {}


async def test_a_hook_only_runs_at_the_junction_it_was_registered_for():
    center = HooksCenter()
    calls: list[str] = []

    async def observable(**_: object) -> None:
        calls.append('observable')

    async def interceptable(**_: object) -> HookResult:
        calls.append('interceptable')
        return HookResult()

    center.register(Junction.TURN_START, observable)
    center.register(Junction.PRE_TOOL_USE, interceptable)
    await center._dispatch(Junction.TURN_END, turn=1)
    await center._dispatch(Junction.POST_TOOL_USE, tool_call=None)
    assert calls == []


async def test_a_hook_registered_by_an_interceptable_hook_runs_from_the_next_trigger():
    center = HooksCenter()
    calls: list[str] = []

    async def latecomer(**_: object) -> HookResult:
        calls.append('latecomer')
        return HookResult()

    async def registrar(**_: object) -> HookResult:
        calls.append('registrar')
        center.register(Junction.PRE_TOOL_USE, latecomer)
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, registrar)
    await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert calls == ['registrar']
    await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert calls == ['registrar', 'registrar', 'latecomer']


async def test_a_hook_registered_by_an_observable_hook_runs_from_the_next_trigger():
    """两类切点在「本轮 hook 集合何时定下」上必须同解，否则同一段代码的行为取决于它挂在哪。"""
    center = HooksCenter()
    calls: list[str] = []

    async def latecomer(**_: object) -> None:
        calls.append('latecomer')

    async def registrar(**_: object) -> None:
        calls.append('registrar')
        center.register(Junction.TURN_START, latecomer)

    center.register(Junction.TURN_START, registrar)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert calls == ['registrar']
    await center._dispatch(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['latecomer', 'registrar', 'registrar']


def test_registering_a_non_junction_non_string_is_rejected():
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ConfigError):
        center.register(7, cb)  # pyright: ignore[reportArgumentType]


async def test_triggering_a_non_junction_non_string_is_rejected():
    """既不是枚举成员也不是字符串时说不出期望、实际与下一步的 KeyError 是最差的反馈。"""
    center = HooksCenter()

    with pytest.raises(ConfigError):
        await center.trigger(7, turn=1)  # pyright: ignore[reportArgumentType]


async def test_a_result_without_skip_is_rejected_rather_than_dropped():
    """合并结论只在 skip 那一支带出 `result`，所以不带 skip 的 result 哪个切点都读不到。静默丢弃让
    作者以为自己顶替了本次操作的产出，而原操作照常执行。"""
    center = HooksCenter()

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    center.register(Junction.PRE_TOOL_USE, substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert Junction.PRE_TOOL_USE.value in excinfo.value.expected
    assert 'substitute' in excinfo.value.received
    assert 'skip=True' in excinfo.value.next_action


async def test_a_later_hook_giving_an_orphan_result_is_still_caught():
    """只检查第一个 hook 会让链条后段静默失效。"""
    center = HooksCenter()

    async def observe(**_: object) -> HookResult:
        return HookResult()

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    center.register(Junction.PRE_TOOL_USE, observe)
    center.register(Junction.PRE_TOOL_USE, substitute)
    with pytest.raises(ContractViolation):
        await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)


async def test_the_chain_reports_every_value_a_key_was_given_not_only_the_last():
    """`modified` 是 last-wins 的结论，只能单向收紧的量（trust）看结论会漏掉链中间那次收紧。基数
    取 3 且收紧落在中间：只看首项或只看末项的实现都必须变红。"""
    center = HooksCenter()

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'result': 'plain'})

    async def second(**_: object) -> HookResult:
        return HookResult(modified={'result': 'tightened', 'args': {}})

    async def third(**_: object) -> HookResult:
        return HookResult(modified={'result': 'loosened'})

    for cb in (first, second, third):
        center.register(Junction.POST_TOOL_USE, cb)
    outcome = await center._dispatch(Junction.POST_TOOL_USE, result='original')
    assert outcome.modified == {'result': 'loosened', 'args': {}}
    assert outcome.rewrites('result') == ['plain', 'tightened', 'loosened']
    assert outcome.rewrites('args') == [{}]
    assert outcome.rewrites('never-touched') == []


async def test_the_rewrites_of_a_skipping_chain_stop_where_the_chain_did():
    center = HooksCenter()

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'result': 'tightened'})

    async def denier(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'result': 'denied'}, result='denied')

    async def never(**_: object) -> HookResult:
        return HookResult(modified={'result': 'unreachable'})

    for cb in (first, denier, never):
        center.register(Junction.PRE_TOOL_USE, cb)
    outcome = await center._dispatch(Junction.PRE_TOOL_USE, tool_call=None)
    assert outcome.rewrites('result') == ['tightened', 'denied']


async def test_mutating_the_returned_rewrites_cannot_reach_the_record_behind_them():
    """收紧判定按这份清单做，而同一个 HookResult 会被读多次；交出内部列表，第一个读者一次
    `append` 就改了后来每个读者看到的历史。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'result': 'once'})

    center.register(Junction.POST_TOOL_USE, rewrite)
    outcome = await center._dispatch(Junction.POST_TOOL_USE, result='original')
    outcome.rewrites('result').append('injected')
    assert outcome.rewrites('result') == ['once']
    assert outcome.history['result'] == ['once']


def test_hook_result_history_defaults_to_empty_and_is_not_shared():
    first = HookResult()
    second = HookResult()
    assert first.history == {}
    first.history['result'] = ['a']
    assert second.history == {}


def test_hook_result_fields_are_the_positional_contract():
    assert [f.name for f in dataclasses.fields(HookResult)] == [
        'skip',
        'modified',
        'result',
        'history',
        'untrusted',
    ]


def test_two_junction_kinds_exist():
    """取值撞车会让成员静默塌成别名；分类决定切点的执行语义，与切点名同属公开 API。"""
    assert {kind.name: kind.value for kind in JunctionKind} == {
        'INTERCEPTABLE': 'interceptable',
        'OBSERVABLE': 'observable',
    }


def test_declaring_extension_junctions_leaves_the_kernel_classifications_untouched():
    """两个集合是对 `Junction` 成员的划分；掺进扩展切点，「新增枚举成员两边都不在即变红」就失效。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    assert INTERCEPTABLE == {
        Junction.PRE_TOOL_USE,
        Junction.POST_TOOL_USE,
        Junction.PRE_LLM_REQUEST,
        Junction.POST_LLM_RESPONSE,
        Junction.CONTEXT_OVERFLOW,
        Junction.LLM_FAILURE,
        Junction.COMPLETION_PROPOSED,
    }
    assert OBSERVABLE == {
        Junction.RUNTIME_SHUTDOWN,
        Junction.AGENT_SPAWN,
        Junction.AGENT_DESPAWN,
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
        Junction.TOOL_CONFIRMATION_REQUIRED,
        Junction.ARTIFACT_REGISTERED,
    }
    assert len(set(Junction)) == 18


async def test_a_kernel_junction_in_neither_classification_is_rejected_at_trigger(
    monkeypatch: pytest.MonkeyPatch,
):
    """分发处不写「不在可拦截即可观察」：那会让新增枚举成员靠遗漏拿到「并行 + 吞异常」语义。
    分类在空 hook 列表检查之前取，所以一个没人挂钩的未归类切点同样要报出来。"""
    monkeypatch.setattr(center_module, 'OBSERVABLE', OBSERVABLE - {Junction.TURN_START})
    with pytest.raises(ContractViolation):
        await HooksCenter()._dispatch(Junction.TURN_START, turn=1)


async def test_an_interceptable_extension_junction_is_serial_and_accumulates_modified():
    """扩展切点与枚举切点共用同一条分发路径：中间件语义必须逐条成立，而不是只有枚举切点才有。
    基数取 3 且顺序既非升序亦非降序，否则排序类变异过不了关。"""
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    seen: list[dict[str, object]] = []

    async def redact(**_: object) -> HookResult:
        return HookResult(modified={'artifact': 'redacted'})

    async def stamp(**kwargs: object) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult(modified={'version': 2})

    async def audit(**kwargs: object) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult(modified={'audited': True})

    for cb in (redact, stamp, audit):
        center.register('artifact:created', cb)
    result = await center.trigger(artifact_created, artifact='raw', kind='spreadsheet')
    assert seen == [
        {'artifact': 'redacted', 'kind': 'spreadsheet'},
        {'artifact': 'redacted', 'kind': 'spreadsheet', 'version': 2},
    ]
    assert result.modified == {'artifact': 'redacted', 'version': 2, 'audited': True}


async def test_an_interceptable_extension_junction_reports_every_value_a_key_was_given():
    """只能单向收紧的量（trust）看 last-wins 的结论会漏掉链中间那次收紧。"""
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'plain'})

    async def second(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'tightened'})

    async def third(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'loosened'})

    for cb in (first, second, third):
        center.register('artifact:created', cb)
    result = await center.trigger(artifact_created, trust='original')
    assert result.rewrites('trust') == ['plain', 'tightened', 'loosened']


async def test_an_interceptable_extension_junctions_skip_stops_the_chain_and_carries_its_result():
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    calls: list[str] = []

    async def deny(**_: object) -> HookResult:
        calls.append('deny')
        return HookResult(skip=True, result='denied')

    async def never(**_: object) -> HookResult:
        calls.append('never')
        return HookResult()

    center.register('artifact:created', deny)
    center.register('artifact:created', never)
    result = await center.trigger(artifact_created, artifact='raw')
    assert calls == ['deny']
    assert (result.skip, result.result) == (True, 'denied')


async def test_an_interceptable_extension_hook_that_raises_propagates():
    """扩展切点上的可拦截 hook 同样是策略；跳过一个失败的策略检查等于放行未经检查的操作。"""
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    boom = RuntimeError('artifact policy blew up')

    async def explodes(**_: object) -> HookResult:
        raise boom

    center.register('artifact:created', explodes)
    with pytest.raises(RuntimeError) as excinfo:
        await center.trigger(artifact_created, artifact='raw')
    assert excinfo.value is boom


async def test_an_orphan_result_at_an_extension_junction_is_rejected_rather_than_dropped():
    """`result` 不带 `skip` 哪个切点都读不到，与切点是枚举还是字符串无关。"""
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    center.register('artifact:created', substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await center.trigger(artifact_created, artifact='raw')
    assert 'artifact:created' in excinfo.value.expected


async def test_an_observable_extension_junction_runs_its_hooks_concurrently():
    """串行执行会卡死在栅栏上；扩展切点声明为可观察就得真拿到并行。"""
    center = HooksCenter()
    metrics_sampled = center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    both_started = asyncio.Barrier(2)

    async def wait_for_the_other(**_: object) -> None:
        await both_started.wait()

    center.register('metrics:sampled', wait_for_the_other)
    center.register('metrics:sampled', wait_for_the_other)
    await asyncio.wait_for(center.trigger(metrics_sampled, value=1), timeout=_TIMEOUT)


async def test_an_observable_extension_hook_that_raises_does_not_break_trigger(
    caplog: pytest.LogCaptureFixture,
):
    center = HooksCenter()
    metrics_sampled = center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    calls: list[str] = []
    boom = RuntimeError('metrics hook blew up')

    async def explodes(**_: object) -> None:
        calls.append('explodes')
        raise boom

    async def survivor(**_: object) -> None:
        calls.append('survivor')

    center.register('metrics:sampled', explodes)
    center.register('metrics:sampled', survivor)
    result = await center.trigger(metrics_sampled, value=1)
    assert sorted(calls) == ['explodes', 'survivor']
    assert (result.skip, result.modified, result.result) == (False, {}, None)
    (record,) = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert 'metrics:sampled' in record.getMessage()


async def test_an_observable_extension_hooks_returned_result_is_discarded_with_a_diagnostic(
    caplog: pytest.LogCaptureFixture,
):
    center = HooksCenter()
    metrics_sampled = center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)

    async def opinionated(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'value': 99}, result='substitute')

    center.register('metrics:sampled', opinionated)
    result = await center.trigger(metrics_sampled, value=1)
    assert (result.skip, result.modified, result.result) == (False, {}, None)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.args == (
        'metrics:sampled',
        (
            'test_an_observable_extension_hooks_returned_result_is_discarded_with_a_diagnostic'
            '.<locals>.opinionated'
        ),
        "HookResult(skip=True, modified=['value'], result='substitute')",
    )


async def test_a_hook_registered_by_an_interceptable_extension_hook_waits_for_the_next_trigger():
    center = HooksCenter()
    artifact_created = center.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    calls: list[str] = []

    async def latecomer(**_: object) -> HookResult:
        calls.append('latecomer')
        return HookResult()

    async def registrar(**_: object) -> HookResult:
        calls.append('registrar')
        center.register('artifact:created', latecomer)
        return HookResult()

    center.register('artifact:created', registrar)
    await center.trigger(artifact_created, artifact='raw')
    assert calls == ['registrar']
    await center.trigger(artifact_created, artifact='raw')
    assert calls == ['registrar', 'registrar', 'latecomer']


async def test_a_hook_registered_by_an_observable_extension_hook_waits_for_the_next_trigger():
    """两条分发路径在「本轮 hook 集合何时定下」上必须同解，扩展切点也一样。"""
    center = HooksCenter()
    metrics_sampled = center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def latecomer(**_: object) -> None:
        calls.append('latecomer')

    async def registrar(**_: object) -> None:
        calls.append('registrar')
        center.register('metrics:sampled', latecomer)

    center.register('metrics:sampled', registrar)
    await center.trigger(metrics_sampled, value=1)
    assert calls == ['registrar']
    await center.trigger(metrics_sampled, value=1)
    assert sorted(calls) == ['latecomer', 'registrar', 'registrar']


async def test_two_owners_declaring_the_same_event_name_get_two_separate_junctions():
    """owner 段存在的第一个理由：两个扩展不该抢同一个名字，也不该收到对方的事件。"""
    center = HooksCenter()
    syndica_office_created = center.declare_extension_junction(
        'syndica-office:created', JunctionKind.OBSERVABLE
    )
    center.declare_extension_junction('syndica-sheets:created', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def office(**_: object) -> None:
        calls.append('office')

    async def sheets(**_: object) -> None:
        calls.append('sheets')

    center.register('syndica-office:created', office)
    center.register('syndica-sheets:created', sheets)
    await center.trigger(syndica_office_created, artifact='raw')
    assert calls == ['office']


async def test_an_extension_junction_and_a_kernel_junction_do_not_share_hooks():
    center = HooksCenter()
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def extension(**_: object) -> None:
        calls.append('extension')

    async def kernel(**_: object) -> None:
        calls.append('kernel')

    center.register('metrics:sampled', extension)
    center.register(Junction.TURN_START, kernel)
    await center._dispatch(Junction.TURN_START, turn=1)
    assert calls == ['kernel']


async def test_declaring_an_extension_junction_twice_is_refused_even_with_the_same_kind():
    """句柄只能有一个持有者：同名同分类也拒，而被拒的那次不碰已挂上的 hook 与原句柄。"""
    center = HooksCenter()
    created = center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    calls: list[str] = []

    async def existing(**_: object) -> HookResult:
        calls.append('existing')
        return HookResult()

    center.register('artifact:created', existing)
    with pytest.raises(ContractViolation) as excinfo:
        center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    assert '只声明一次' in excinfo.value.expected
    assert '装配期之外的代码' in excinfo.value.received
    await center.trigger(created, artifact='raw')
    assert calls == ['existing']


async def test_redeclaring_an_extension_junction_with_a_different_kind_is_rejected():
    """今天没有注册者标识可比，分类冲突是唯一能观测到的抢名信号：静默让先声明者胜，会让后来者的
    可拦截 hook 按可观察跑，异常就被吞了。"""
    center = HooksCenter()
    created = center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    with pytest.raises(ContractViolation):
        center.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)

    boom = RuntimeError('artifact policy blew up')

    async def explodes(**_: object) -> HookResult:
        raise boom

    center.register('artifact:created', explodes)
    with pytest.raises(RuntimeError) as excinfo:
        await center.trigger(created, artifact='raw')
    assert excinfo.value is boom


def test_declaring_an_extension_junction_with_a_kind_lookalike_string_is_rejected():
    """`'observable'` 与两个成员都不相等，收下它会让分发 `is` 比较落进可观察那一侧。"""
    center = HooksCenter()
    name = 'artifact:created'
    with pytest.raises(ConfigError):
        center.declare_extension_junction(name, 'observable')  # pyright: ignore[reportArgumentType]


def test_declaring_an_extension_junction_with_a_non_string_name_is_rejected():
    center = HooksCenter()
    kind = JunctionKind.OBSERVABLE
    with pytest.raises(ConfigError):
        center.declare_extension_junction(7, kind)  # pyright: ignore[reportArgumentType]


def test_an_extension_junction_name_without_a_separator_is_rejected():
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction('created', JunctionKind.OBSERVABLE)


def test_an_extension_junction_name_with_more_than_one_separator_is_rejected():
    """多级命名空间日后放宽不是破坏性变更，现在收下了却再也收不回来。"""
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction('artifact:created:v2', JunctionKind.OBSERVABLE)


def test_an_extension_junction_name_with_an_empty_owner_is_rejected():
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction(':created', JunctionKind.OBSERVABLE)


def test_an_extension_junction_name_with_an_empty_event_is_rejected():
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction('artifact:', JunctionKind.OBSERVABLE)


def test_an_empty_extension_junction_name_is_rejected():
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction('', JunctionKind.OBSERVABLE)


def test_an_extension_junction_name_with_whitespace_anywhere_is_rejected():
    center = HooksCenter()
    for name in (
        ' artifact:created',
        'artifact:created ',
        'my artifact:created',
        'artifact: created',
        'artifact:\tcreated',
    ):
        with pytest.raises(ConfigError):
            center.declare_extension_junction(name, JunctionKind.OBSERVABLE)


async def test_a_whitespace_padded_name_does_not_resolve_to_the_declared_one():
    """刻意不自动 strip：两种拼法在日志与错误消息里长得一样，静默合并会让日后绑定注册者标识时
    没有唯一的规范拼法可比。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def cb(**_: object) -> None:
        calls.append('cb')

    center.register('artifact:created', cb)
    with pytest.raises(ConfigError):
        await center.trigger(' artifact:created', artifact='raw')  # pyright: ignore[reportArgumentType]
    assert calls == []


async def test_extension_junction_names_are_case_sensitive():
    """名字计入公开 API，折叠大小写等于让两种拼法映射到同一个切点。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    with pytest.raises(ContractViolation):
        await center.trigger('artifact:Created', artifact='raw')  # pyright: ignore[reportArgumentType]


def test_declaring_a_kernel_junctions_string_value_as_an_extension_is_rejected():
    center = HooksCenter()
    with pytest.raises(ConfigError):
        center.declare_extension_junction('turn_start', JunctionKind.OBSERVABLE)


def test_registering_a_kernel_junction_by_its_string_value_is_rejected():
    """字符串与枚举通到同一个切点，会让按枚举触发时读不到按字符串注册的 hook——静默空转。"""
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ConfigError):
        center.register('turn_start', cb)


async def test_triggering_a_kernel_junction_by_its_string_value_neither_fires_nor_aliases():
    center = HooksCenter()
    calls: list[str] = []

    async def cb(**_: object) -> None:
        calls.append('cb')

    center.register(Junction.TURN_START, cb)
    with pytest.raises(ConfigError):
        await center.trigger('turn_start', turn=1)  # pyright: ignore[reportArgumentType]
    assert calls == []


def test_registering_an_undeclared_extension_junction_is_rejected():
    """分类没有默认值，所以没有「先挂上、分类以后再说」这条路：那个 hook 会永不触发。"""
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        center.register('artifact:created', cb)


async def test_triggering_an_undeclared_extension_junction_is_rejected():
    center = HooksCenter()
    with pytest.raises(ContractViolation):
        await center.trigger('artifact:created', artifact='raw')  # pyright: ignore[reportArgumentType]


def test_extension_junctions_are_not_shared_between_centers():
    """声明挂在实例上而非模块全局：两个 Runtime 各有一个 HooksCenter，串台会让一个能力的切点
    在另一个 Runtime 里凭空存在。"""
    declaring = HooksCenter()
    declaring.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        HooksCenter().register('artifact:created', cb)


def _appender(log: list[str], label: str) -> HookCallback:
    async def record(**_: object) -> None:
        log.append(label)

    return record


async def test_the_inner_layers_hooks_run_before_the_outer_ones():
    """spec §5.3：Agent 局部先于 Runtime 级。基数 3，且顺序既非升序亦非降序、也不等于反转。"""
    log: list[str] = []
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    inner.register(Junction.PRE_TOOL_USE, _appender(log, 'beta'))
    outer.register(Junction.PRE_TOOL_USE, _appender(log, 'alpha'))
    outer.register(Junction.PRE_TOOL_USE, _appender(log, 'gamma'))
    await inner._dispatch(Junction.PRE_TOOL_USE, args={})
    assert log == ['beta', 'alpha', 'gamma']


async def test_three_layers_run_from_the_innermost_outwards():
    """装配链可以比两层长（一个 Agent 的外层还有外层）；顺序仍是由内向外。"""
    log: list[str] = []
    outermost = HooksCenter()
    middle = HooksCenter(outer=outermost)
    innermost = HooksCenter(outer=middle)
    middle.register(Junction.PRE_TOOL_USE, _appender(log, 'middle'))
    outermost.register(Junction.PRE_TOOL_USE, _appender(log, 'outermost'))
    innermost.register(Junction.PRE_TOOL_USE, _appender(log, 'innermost'))
    await innermost._dispatch(Junction.PRE_TOOL_USE, args={})
    assert log == ['innermost', 'middle', 'outermost']


async def test_the_outer_center_never_sees_the_inner_ones_hooks():
    """外层不知道内层的存在：一个插件的行为不该因为某个 Agent 挂了局部钩子而改变。"""
    log: list[str] = []
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    inner.register(Junction.PRE_TOOL_USE, _appender(log, 'inner'))
    outer.register(Junction.PRE_TOOL_USE, _appender(log, 'outer'))
    await outer._dispatch(Junction.PRE_TOOL_USE, args={})
    assert log == ['outer']


async def test_two_inner_centers_sharing_an_outer_do_not_see_each_other():
    """两个 Agent 共用一个 Runtime 中心；一个挂的局部钩子跑进另一个的链里就是串线。"""
    log: list[str] = []
    outer = HooksCenter()
    first = HooksCenter(outer=outer)
    second = HooksCenter(outer=outer)
    first.register(Junction.PRE_TOOL_USE, _appender(log, 'first'))
    second.register(Junction.PRE_TOOL_USE, _appender(log, 'second'))
    outer.register(Junction.PRE_TOOL_USE, _appender(log, 'shared'))
    await first._dispatch(Junction.PRE_TOOL_USE, args={})
    assert log == ['first', 'shared']


async def test_an_inner_hooks_modification_becomes_the_outer_hooks_input():
    """两层是一条链而不是两条：跑两遍的话外层看到的是原始参数，一次局部改写就白做了。"""
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    seen: list[object] = []

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'args': {'command': 'rewritten'}})

    async def observe(**kwargs: object) -> HookResult:
        seen.append(kwargs.get('args'))
        return HookResult()

    inner.register(Junction.PRE_TOOL_USE, rewrite)
    outer.register(Junction.PRE_TOOL_USE, observe)
    result = await inner._dispatch(Junction.PRE_TOOL_USE, args={'command': 'original'})
    assert seen == [{'command': 'rewritten'}]
    assert result.modified == {'args': {'command': 'rewritten'}}


async def test_an_inner_skip_terminates_the_outer_layers_hooks_too():
    """skip 只终止本层的话，Runtime 级的 hook 会跑在一个已经被顶替掉的操作上。"""
    log: list[str] = []
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)

    async def veto(**_: object) -> HookResult:
        log.append('veto')
        return HookResult(skip=True, result='vetoed')

    inner.register(Junction.PRE_TOOL_USE, veto)
    outer.register(Junction.PRE_TOOL_USE, _appender(log, 'outer'))
    result = await inner._dispatch(Junction.PRE_TOOL_USE, args={})
    assert log == ['veto']
    assert (result.skip, result.result) == (True, 'vetoed')


async def test_an_outer_skip_carries_out_the_inner_layers_modifications():
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)

    async def redact(**_: object) -> HookResult:
        return HookResult(modified={'args': 'redacted'})

    async def veto(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'audited': True}, result='vetoed')

    inner.register(Junction.PRE_TOOL_USE, redact)
    outer.register(Junction.PRE_TOOL_USE, veto)
    result = await inner._dispatch(Junction.PRE_TOOL_USE, args='original')
    assert result.skip is True
    assert result.modified == {'args': 'redacted', 'audited': True}


async def test_the_chain_records_every_value_a_key_was_given_across_both_layers():
    """只能单向收紧的量（trust）看 last-wins 的结论会漏掉链中间那次收紧——分层之后那次收紧可能
    落在内层，而后写的外层 hook 交出的是一个带默认值的新结果。"""
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)

    async def tighten(**_: object) -> HookResult:
        return HookResult(modified={'result': 'untrusted'})

    async def summarise(**_: object) -> HookResult:
        return HookResult(modified={'result': 'summarised'})

    inner.register(Junction.POST_TOOL_USE, tighten)
    outer.register(Junction.POST_TOOL_USE, summarise)
    result = await inner._dispatch(Junction.POST_TOOL_USE, result='original')
    assert result.modified == {'result': 'summarised'}
    assert result.rewrites('result') == ['untrusted', 'summarised']


async def test_an_orphan_result_from_an_outer_hook_is_rejected_by_the_inner_trigger():
    """`result` 不带 `skip` 谁都读不到，这条与 hook 挂在哪一层无关。"""
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    outer.register(Junction.PRE_TOOL_USE, substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await inner._dispatch(Junction.PRE_TOOL_USE, args={})
    assert 'substitute' in excinfo.value.received


async def test_both_layers_observable_hooks_run_concurrently():
    """分层不得把可观察切点串起来：一条链跑完才跑另一条会卡死在栅栏上。"""
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    both_started = asyncio.Barrier(2)

    async def wait_for_the_other(**_: object) -> None:
        await both_started.wait()

    inner.register(Junction.TURN_START, wait_for_the_other)
    outer.register(Junction.TURN_START, wait_for_the_other)
    await asyncio.wait_for(inner._dispatch(Junction.TURN_START, turn=1), timeout=_TIMEOUT)


async def test_an_extension_junction_declared_on_the_outer_center_can_be_hooked_from_the_inner():
    """分类由声明者给出，而声明在外层——局部注册必须能沿装配链找到它，否则 register 就报错。"""
    outer = HooksCenter()
    artifact_created = outer.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    inner = HooksCenter(outer=outer)
    log: list[str] = []

    async def local(**_: object) -> HookResult:
        log.append('local')
        return HookResult()

    inner.register('artifact:created', local)
    await inner.trigger(artifact_created, artifact='raw')
    assert log == ['local']


async def test_an_extension_junction_declared_on_the_outer_keeps_its_kind_in_the_inner_layer():
    """分类沿链取：内层若按默认或按本层的空表重新推断，可拦截 hook 会按可观察跑，异常被吞掉。"""
    outer = HooksCenter()
    artifact_created = outer.declare_extension_junction(
        'artifact:created', JunctionKind.INTERCEPTABLE
    )
    inner = HooksCenter(outer=outer)
    boom = RuntimeError('artifact policy blew up')

    async def explodes(**_: object) -> HookResult:
        raise boom

    inner.register('artifact:created', explodes)
    with pytest.raises(RuntimeError) as excinfo:
        await inner.trigger(artifact_created, artifact='raw')
    assert excinfo.value is boom


def test_redeclaring_an_outer_extension_junction_with_a_different_kind_is_rejected():
    """冲突检查只看本层的话，同一个名字在两层上有两套执行语义，而 hook 按哪套跑取决于它挂在哪。"""
    outer = HooksCenter()
    outer.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    inner = HooksCenter(outer=outer)
    with pytest.raises(ContractViolation):
        inner.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)


def test_redeclaring_an_outer_extension_junction_with_the_same_kind_is_refused():
    outer = HooksCenter()
    outer.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    inner = HooksCenter(outer=outer)
    with pytest.raises(ContractViolation):
        inner.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)


def test_the_undeclared_extension_message_lists_the_whole_chains_declarations():
    """读者要看的是「我能挂哪些」，而答案分散在两层上；只列本层等于把外层声明的那些说成不存在。"""
    outer = HooksCenter()
    outer.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    inner = HooksCenter(outer=outer)
    inner.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation) as excinfo:
        inner.register('artifact:removed', cb)
    assert "['artifact:created', 'metrics:sampled']" in excinfo.value.received


def test_an_extension_junction_declared_on_the_inner_center_stays_invisible_to_the_outer():
    outer = HooksCenter()
    inner = HooksCenter(outer=outer)
    inner.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        outer.register('artifact:created', cb)


def _writes_via_setitem(args: ReadOnlyKwarg) -> None:
    args['command'] = 'sudo rm -rf /'


def _writes_via_delitem(args: ReadOnlyKwarg) -> None:
    del args['command']


def _writes_via_ior(args: ReadOnlyKwarg) -> None:
    # 拒绝写入面意味着 `__ior__` 交回 NoReturn，于是这句在静态检查眼里「赋不回去」——而它恰恰是
    # hook 作者会写下的那一句，运行期必须拒。
    args |= {'command': 'sudo rm -rf /'}  # pyright: ignore[reportOperatorIssue]


def _writes_via_clear(args: ReadOnlyKwarg) -> None:
    args.clear()


def _writes_via_pop(args: ReadOnlyKwarg) -> None:
    args.pop('command')


def _writes_via_pop_with_default(args: ReadOnlyKwarg) -> None:
    args.pop('absent', None)


def _writes_via_popitem(args: ReadOnlyKwarg) -> None:
    args.popitem()


def _writes_via_setdefault(args: ReadOnlyKwarg) -> None:
    args.setdefault('command', 'sudo rm -rf /')


def _writes_via_update(args: ReadOnlyKwarg) -> None:
    args.update({'command': 'sudo rm -rf /'})


def _writes_via_update_kwargs(args: ReadOnlyKwarg) -> None:
    args.update(command='sudo rm -rf /')


@pytest.mark.parametrize(
    'write',
    [
        _writes_via_setitem,
        _writes_via_delitem,
        _writes_via_ior,
        _writes_via_clear,
        _writes_via_pop,
        _writes_via_pop_with_default,
        _writes_via_popitem,
        _writes_via_setdefault,
        _writes_via_update,
        _writes_via_update_kwargs,
    ],
    ids=[
        'setitem',
        'delitem',
        'ior',
        'clear',
        'pop',
        'pop-with-default',
        'popitem',
        'setdefault',
        'update',
        'update-kwargs',
    ],
)
def test_every_write_entry_point_of_a_read_only_kwarg_is_refused(
    write: Callable[[ReadOnlyKwarg], None],
):
    """整个写入面都要拒。漏掉一个入口，就地改就沿那个入口原样回来，而它躲得过作者判据。"""
    args = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', {'command': 'ls -la'})
    with pytest.raises(ContractViolation) as excinfo:
        write(args)
    assert Junction.PRE_TOOL_USE.value in excinfo.value.expected
    assert 'HookResult(modified=' in excinfo.value.next_action
    assert args == {'command': 'ls -la'}


def test_the_read_surface_of_a_read_only_kwarg_is_untouched():
    """读要一字不改地照常：hook 得能看参数、能拿它拼出新的一份。键序取基数 3。"""
    args = ReadOnlyKwarg(
        Junction.PRE_TOOL_USE, 'args', {'command': 'ls -la', 'cwd': '/tmp', 'shell': 'bash'}
    )
    assert isinstance(args, dict)
    assert args == {'command': 'ls -la', 'cwd': '/tmp', 'shell': 'bash'}
    assert args['command'] == 'ls -la'
    assert args.get('absent', 'fallback') == 'fallback'
    assert 'cwd' in args
    assert len(args) == 3
    assert list(args) == ['command', 'cwd', 'shell']
    assert list(args.items()) == [('command', 'ls -la'), ('cwd', '/tmp'), ('shell', 'bash')]
    assert list(args.keys()) == ['command', 'cwd', 'shell']
    assert list(args.values()) == ['ls -la', '/tmp', 'bash']
    assert list(reversed(args)) == ['shell', 'cwd', 'command']


@pytest.mark.parametrize(
    'duplicate',
    [
        dict,
        lambda args: {**args},
        lambda args: args.copy(),
        copy.copy,
        copy.deepcopy,
        lambda args: pickle.loads(pickle.dumps(args)),
        lambda args: args | {},
        lambda args: {} | args,
    ],
    ids=['dict', 'splat', 'copy-method', 'copy', 'deepcopy', 'pickle', 'or', 'ror'],
)
def test_a_copy_taken_out_of_a_read_only_kwarg_is_a_plain_writable_dict(
    duplicate: Callable[[ReadOnlyKwarg], dict[str, object]],
):
    """错误消息给的下一步是「拼一份新的 dict 再交出去」；那条路不通的话这条消息就是空话。

    协议层（`copy` / `deepcopy` / `pickle`）与下标一样是**读**，而 `copy._reconstruct` 往新建的那
    一份里逐键赋值：只拦 `__setitem__` 的包装会把它们拒掉，并报出一条说作者就地写了的假消息。"""
    args = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', {'command': 'ls -la'})
    copied = duplicate(args)
    assert type(copied) is dict
    copied['command'] = 'rewritten'
    assert copied == {'command': 'rewritten'}
    assert args == {'command': 'ls -la'}


def test_a_deep_copy_of_a_read_only_kwarg_is_independent_all_the_way_down():
    """视图只护顶层，`{**args}` 又是浅拷贝，所以 hook 作者要自己补上嵌套那道缺口时唯一的写法就是
    `copy.deepcopy(args)`。拒掉它等于把唯一的自助路堵死。"""
    args = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', {'config': {'flags': ['-f']}})
    copied = copy.deepcopy(args)
    copied['config']['flags'].append('--force')
    assert args == {'config': {'flags': ['-f']}}
    assert copied == {'config': {'flags': ['-f', '--force']}}


def test_a_shallow_copy_of_a_read_only_kwarg_still_shares_its_nested_containers():
    """深浅两条路都从同一个重建钩子走，所以「浅拷贝仍然共享嵌套容器」这件事要单独钉住：`copy.copy`
    悄悄变深会让 D-40 那道嵌套缺口看起来已经关掉。"""
    args = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', {'config': {'flags': ['-f']}})
    assert copy.copy(args)['config'] is args['config']


def test_fromkeys_on_a_read_only_kwarg_hands_back_a_plain_dict():
    """继承来的 `fromkeys` 拿 `cls()` 建实例，撞上三参构造报一条指着 junction / key 缺失的
    TypeError——而调用方要的只是一份新映射。"""
    made = ReadOnlyKwarg.fromkeys(['command', 'cwd'])
    assert type(made) is dict
    made['command'] = 'ls'
    assert made == {'command': 'ls', 'cwd': None}


def test_a_read_only_kwarg_keeps_no_attribute_dict_for_a_hook_to_repoint_it():
    """错误消息点名的切点与 kwarg 决定读者去查哪一处。它们可写的话，一个 hook 能把这条诊断指向另一个
    切点，而这条消息是这次违反留下的全部痕迹。"""
    args = ReadOnlyKwarg(Junction.PRE_TOOL_USE, 'args', {'command': 'ls -la'})
    with pytest.raises(TypeError):
        vars(args)
    with pytest.raises(AttributeError):
        args._other = 'anything'  # pyright: ignore[reportAttributeAccessIssue]


def test_a_read_only_kwarg_names_the_junction_the_kwarg_and_the_attempted_key():
    """一个 hook 手里有好几个 kwarg，将来还会不止一个被护起来：不说清是哪一个被就地写了，作者
    只能逐个试。切点进 expected，被护的那个 kwarg 两栏都要有，被写的那个键进 received。"""
    args = ReadOnlyKwarg('artifact:created', 'artifact', {'path': 'a.md'})
    with pytest.raises(ContractViolation) as excinfo:
        args['path'] = 'b.md'
    assert 'artifact:created' in excinfo.value.expected
    assert "'artifact'" in excinfo.value.expected
    assert "'artifact'" in excinfo.value.received
    assert "'path'" in excinfo.value.received


def test_an_outer_that_is_not_a_hooks_center_is_rejected_at_construction():
    """错配的症状否则是第一次 trigger 抛一条指着 HooksCenter 内部的 AttributeError，而那时调用栈
    里已经没有写下这个参数的那一行。"""
    with pytest.raises(ConfigError) as excinfo:
        HooksCenter(outer=object())  # pyright: ignore[reportArgumentType]
    assert 'HooksCenter' in excinfo.value.expected
