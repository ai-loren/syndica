import asyncio
import logging

import pytest

from syndica.kernel.errors import ConfigError
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


async def test_interceptable_hook_can_modify_kwargs():
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
    assert result.get('request', 'original') == 'rewritten'


async def test_modified_kwargs_reach_the_next_hook():
    """中间件语义的全部：只让第一个 hook 的改动生效，链条就无法组合。"""
    center = HooksCenter()
    seen: list[object] = []

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'request': 'from-first'})

    async def second(**kwargs: object) -> HookResult:
        seen.append(kwargs['request'])
        return HookResult()

    center.register(Junction.PRE_LLM_REQUEST, first)
    center.register(Junction.PRE_LLM_REQUEST, second)
    await center.trigger(Junction.PRE_LLM_REQUEST, request='original')
    assert seen == ['from-first']


async def test_modified_reports_only_the_changed_keys():
    """调用方靠 `modified` 分辨哪些参数被改过；回整份 kwargs 就分辨不出来了。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'request': 'rewritten'})

    center.register(Junction.PRE_LLM_REQUEST, rewrite)
    result = await center.trigger(Junction.PRE_LLM_REQUEST, request='original', model='gpt')
    assert result.modified == {'request': 'rewritten'}


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


def test_registering_unknown_junction_is_rejected():
    center = HooksCenter()

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ConfigError) as excinfo:
        center.register('turn_start', cb)  # pyright: ignore[reportArgumentType]
    assert 'Junction' in excinfo.value.next_action
