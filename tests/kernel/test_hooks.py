import asyncio
import logging

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks import INTERCEPTABLE, OBSERVABLE, HookResult, HooksCenter, Junction

# 并发用例回归时应当变红，而不是把 CI 挂住。
_TIMEOUT = 1.0


def test_eleven_junctions_exist():
    """取值撞车会让成员静默塌成别名；切点名计入公开 API。"""
    assert {junction.name: junction.value for junction in Junction} == {
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'PRE_TOOL_USE': 'pre_tool_use',
        'POST_TOOL_USE': 'post_tool_use',
        'PRE_LLM_REQUEST': 'pre_llm_request',
        'POST_LLM_RESPONSE': 'post_llm_response',
        'CONTEXT_OVERFLOW': 'context_overflow',
        'LLM_FAILURE': 'llm_failure',
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
    }
    assert OBSERVABLE == {
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
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
    await center.trigger(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['first', 'second']


async def test_observable_hooks_run_concurrently():
    """一个慢的可观察 hook 不得把同一切点上的其他 hook 串起来等；串行执行会卡死在栅栏上。"""
    center = HooksCenter()
    both_started = asyncio.Barrier(2)

    async def wait_for_the_other(**_: object) -> None:
        await both_started.wait()

    center.register(Junction.TURN_START, wait_for_the_other)
    center.register(Junction.TURN_START, wait_for_the_other)
    await asyncio.wait_for(center.trigger(Junction.TURN_START, turn=1), timeout=_TIMEOUT)


async def test_an_observable_hooks_returned_result_is_discarded():
    """回调类型对所有切点都是 `HookResult | None`，在可观察切点上返回它不报错也不生效。"""
    center = HooksCenter()
    calls: list[str] = []

    async def opinionated(**_: object) -> HookResult:
        calls.append('opinionated')
        return HookResult(skip=True, modified={'turn': 99}, result='substitute')

    async def after(**_: object) -> None:
        calls.append('after')

    center.register(Junction.TURN_START, opinionated)
    center.register(Junction.TURN_START, after)
    result = await center.trigger(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['after', 'opinionated']
    assert (result.skip, result.modified, result.result) == (False, {}, None)


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
    result = await center.trigger(Junction.TURN_START, turn=1)
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
        await center.trigger(Junction.TURN_START, turn=1)
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
    await center.trigger(Junction.TURN_START, turn=1)
    assert [record.exc_info[1] for record in caplog.records if record.exc_info] == [
        first_boom,
        second_boom,
    ]


async def test_interceptable_hook_can_modify_kwargs():
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
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
    await center.trigger(Junction.PRE_LLM_REQUEST, request='original', model='gpt')
    assert seen == [{'request': 'from-first', 'model': 'gpt'}]


async def test_modified_reports_only_the_changed_keys():
    """调用方靠 `modified` 分辨哪些参数被改过；回整份 kwargs 就分辨不出来了。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original', model='gpt')
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
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
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
    result = await center.trigger(Junction.PRE_TOOL_USE, request='original', tool_call='shell')
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
    result = await center.trigger(Junction.PRE_TOOL_USE, request='original')
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
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
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
    result = await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
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
        await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
    assert excinfo.value is boom
    assert calls == ['explodes']


async def test_trigger_with_no_hooks_returns_empty_result():
    center = HooksCenter()
    result = await center.trigger(Junction.TURN_END, turn=1)
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
    await center.trigger(Junction.TURN_END, turn=1)
    await center.trigger(Junction.POST_TOOL_USE, tool_call=None)
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
    await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
    assert calls == ['registrar']
    await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
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
    await center.trigger(Junction.TURN_START, turn=1)
    assert calls == ['registrar']
    await center.trigger(Junction.TURN_START, turn=1)
    assert sorted(calls) == ['latecomer', 'registrar', 'registrar']


def test_registering_unknown_junction_is_rejected():
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ConfigError) as excinfo:
        center.register('turn_start', cb)  # pyright: ignore[reportArgumentType]
    assert 'Junction' in excinfo.value.expected
    assert 'turn_start' in excinfo.value.received
    assert 'Junction' in excinfo.value.next_action


async def test_triggering_unknown_junction_is_rejected():
    """裸字符串在 trigger 上同样是配置错误；KeyError 说不出期望、实际与下一步。"""
    center = HooksCenter()

    with pytest.raises(ConfigError) as excinfo:
        await center.trigger('turn_start', turn=1)  # pyright: ignore[reportArgumentType]
    assert 'Junction' in excinfo.value.expected
    assert 'turn_start' in excinfo.value.received


async def test_a_result_without_skip_is_rejected_rather_than_dropped():
    """合并结论只在 skip 那一支带出 `result`，所以不带 skip 的 result 哪个切点都读不到。静默丢弃让
    作者以为自己顶替了本次操作的产出，而原操作照常执行。"""
    center = HooksCenter()

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    center.register(Junction.PRE_TOOL_USE, substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
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
        await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
