import asyncio
import dataclasses
import logging

import pytest

from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.hooks import (
    INTERCEPTABLE,
    OBSERVABLE,
    HookResult,
    HooksCenter,
    Junction,
    JunctionKind,
)
from syndica.kernel.hooks import center as center_module

# 并发用例回归时应当变红，而不是把 CI 挂住。
_TIMEOUT = 1.0


def test_twelve_junctions_exist():
    """取值撞车会让成员静默塌成别名；切点名计入公开 API。"""
    assert {junction.name: junction.value for junction in Junction} == {
        'AGENT_START': 'agent_start',
        'AGENT_END': 'agent_end',
        'AGENT_ERROR': 'agent_error',
        'TURN_START': 'turn_start',
        'TURN_END': 'turn_end',
        'MESSAGE_INJECTED': 'message_injected',
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
        Junction.MESSAGE_INJECTED,
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
    outcome = await center.trigger(Junction.POST_TOOL_USE, result='original')
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
    outcome = await center.trigger(Junction.PRE_TOOL_USE, tool_call=None)
    assert outcome.rewrites('result') == ['tightened', 'denied']


async def test_mutating_the_returned_rewrites_cannot_reach_the_record_behind_them():
    """收紧判定按这份清单做，而同一个 HookResult 会被读多次；交出内部列表，第一个读者一次
    `append` 就改了后来每个读者看到的历史。"""
    center = HooksCenter()

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'result': 'once'})

    center.register(Junction.POST_TOOL_USE, rewrite)
    outcome = await center.trigger(Junction.POST_TOOL_USE, result='original')
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
    }
    assert OBSERVABLE == {
        Junction.AGENT_START,
        Junction.AGENT_END,
        Junction.AGENT_ERROR,
        Junction.TURN_START,
        Junction.TURN_END,
        Junction.MESSAGE_INJECTED,
    }
    assert len(set(Junction)) == 12


async def test_a_kernel_junction_in_neither_classification_is_rejected_at_trigger(
    monkeypatch: pytest.MonkeyPatch,
):
    """分发处不写「不在可拦截即可观察」：那会让新增枚举成员靠遗漏拿到「并行 + 吞异常」语义。
    分类在空 hook 列表检查之前取，所以一个没人挂钩的未归类切点同样要报出来。"""
    monkeypatch.setattr(center_module, 'OBSERVABLE', OBSERVABLE - {Junction.TURN_START})
    with pytest.raises(ContractViolation):
        await HooksCenter().trigger(Junction.TURN_START, turn=1)


async def test_an_interceptable_extension_junction_is_serial_and_accumulates_modified():
    """扩展切点与枚举切点共用同一条分发路径：中间件语义必须逐条成立，而不是只有枚举切点才有。
    基数取 3 且顺序既非升序亦非降序，否则排序类变异过不了关。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
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
    result = await center.trigger('artifact:created', artifact='raw', kind='spreadsheet')
    assert seen == [
        {'artifact': 'redacted', 'kind': 'spreadsheet'},
        {'artifact': 'redacted', 'kind': 'spreadsheet', 'version': 2},
    ]
    assert result.modified == {'artifact': 'redacted', 'version': 2, 'audited': True}


async def test_an_interceptable_extension_junction_reports_every_value_a_key_was_given():
    """只能单向收紧的量（trust）看 last-wins 的结论会漏掉链中间那次收紧。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)

    async def first(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'plain'})

    async def second(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'tightened'})

    async def third(**_: object) -> HookResult:
        return HookResult(modified={'trust': 'loosened'})

    for cb in (first, second, third):
        center.register('artifact:created', cb)
    result = await center.trigger('artifact:created', trust='original')
    assert result.rewrites('trust') == ['plain', 'tightened', 'loosened']


async def test_an_interceptable_extension_junctions_skip_stops_the_chain_and_carries_its_result():
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    calls: list[str] = []

    async def deny(**_: object) -> HookResult:
        calls.append('deny')
        return HookResult(skip=True, result='denied')

    async def never(**_: object) -> HookResult:
        calls.append('never')
        return HookResult()

    center.register('artifact:created', deny)
    center.register('artifact:created', never)
    result = await center.trigger('artifact:created', artifact='raw')
    assert calls == ['deny']
    assert (result.skip, result.result) == (True, 'denied')


async def test_an_interceptable_extension_hook_that_raises_propagates():
    """扩展切点上的可拦截 hook 同样是策略；跳过一个失败的策略检查等于放行未经检查的操作。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    boom = RuntimeError('artifact policy blew up')

    async def explodes(**_: object) -> HookResult:
        raise boom

    center.register('artifact:created', explodes)
    with pytest.raises(RuntimeError) as excinfo:
        await center.trigger('artifact:created', artifact='raw')
    assert excinfo.value is boom


async def test_an_orphan_result_at_an_extension_junction_is_rejected_rather_than_dropped():
    """`result` 不带 `skip` 哪个切点都读不到，与切点是枚举还是字符串无关。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)

    async def substitute(**_: object) -> HookResult:
        return HookResult(result='substitute')

    center.register('artifact:created', substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await center.trigger('artifact:created', artifact='raw')
    assert 'artifact:created' in excinfo.value.expected


async def test_an_observable_extension_junction_runs_its_hooks_concurrently():
    """串行执行会卡死在栅栏上；扩展切点声明为可观察就得真拿到并行。"""
    center = HooksCenter()
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    both_started = asyncio.Barrier(2)

    async def wait_for_the_other(**_: object) -> None:
        await both_started.wait()

    center.register('metrics:sampled', wait_for_the_other)
    center.register('metrics:sampled', wait_for_the_other)
    await asyncio.wait_for(center.trigger('metrics:sampled', value=1), timeout=_TIMEOUT)


async def test_an_observable_extension_hook_that_raises_does_not_break_trigger(
    caplog: pytest.LogCaptureFixture,
):
    center = HooksCenter()
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    calls: list[str] = []
    boom = RuntimeError('metrics hook blew up')

    async def explodes(**_: object) -> None:
        calls.append('explodes')
        raise boom

    async def survivor(**_: object) -> None:
        calls.append('survivor')

    center.register('metrics:sampled', explodes)
    center.register('metrics:sampled', survivor)
    result = await center.trigger('metrics:sampled', value=1)
    assert sorted(calls) == ['explodes', 'survivor']
    assert (result.skip, result.modified, result.result) == (False, {}, None)
    (record,) = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert 'metrics:sampled' in record.getMessage()


async def test_an_observable_extension_hooks_returned_result_is_discarded():
    center = HooksCenter()
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)

    async def opinionated(**_: object) -> HookResult:
        return HookResult(skip=True, modified={'value': 99}, result='substitute')

    center.register('metrics:sampled', opinionated)
    result = await center.trigger('metrics:sampled', value=1)
    assert (result.skip, result.modified, result.result) == (False, {}, None)


async def test_a_hook_registered_by_an_interceptable_extension_hook_waits_for_the_next_trigger():
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    calls: list[str] = []

    async def latecomer(**_: object) -> HookResult:
        calls.append('latecomer')
        return HookResult()

    async def registrar(**_: object) -> HookResult:
        calls.append('registrar')
        center.register('artifact:created', latecomer)
        return HookResult()

    center.register('artifact:created', registrar)
    await center.trigger('artifact:created', artifact='raw')
    assert calls == ['registrar']
    await center.trigger('artifact:created', artifact='raw')
    assert calls == ['registrar', 'registrar', 'latecomer']


async def test_a_hook_registered_by_an_observable_extension_hook_waits_for_the_next_trigger():
    """两条分发路径在「本轮 hook 集合何时定下」上必须同解，扩展切点也一样。"""
    center = HooksCenter()
    center.declare_extension_junction('metrics:sampled', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def latecomer(**_: object) -> None:
        calls.append('latecomer')

    async def registrar(**_: object) -> None:
        calls.append('registrar')
        center.register('metrics:sampled', latecomer)

    center.register('metrics:sampled', registrar)
    await center.trigger('metrics:sampled', value=1)
    assert calls == ['registrar']
    await center.trigger('metrics:sampled', value=1)
    assert sorted(calls) == ['latecomer', 'registrar', 'registrar']


async def test_two_owners_declaring_the_same_event_name_get_two_separate_junctions():
    """owner 段存在的第一个理由：两个扩展不该抢同一个名字，也不该收到对方的事件。"""
    center = HooksCenter()
    center.declare_extension_junction('syndica-office:created', JunctionKind.OBSERVABLE)
    center.declare_extension_junction('syndica-sheets:created', JunctionKind.OBSERVABLE)
    calls: list[str] = []

    async def office(**_: object) -> None:
        calls.append('office')

    async def sheets(**_: object) -> None:
        calls.append('sheets')

    center.register('syndica-office:created', office)
    center.register('syndica-sheets:created', sheets)
    await center.trigger('syndica-office:created', artifact='raw')
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
    await center.trigger(Junction.TURN_START, turn=1)
    assert calls == ['kernel']


async def test_declaring_the_same_extension_junction_with_the_same_kind_keeps_its_hooks():
    """后置条件已成立即成功；但重复声明不得把已挂上的 hook 抹掉。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    calls: list[str] = []

    async def existing(**_: object) -> HookResult:
        calls.append('existing')
        return HookResult()

    center.register('artifact:created', existing)
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    await center.trigger('artifact:created', artifact='raw')
    assert calls == ['existing']


async def test_redeclaring_an_extension_junction_with_a_different_kind_is_rejected():
    """今天没有注册者标识可比，分类冲突是唯一能观测到的抢名信号：静默让先声明者胜，会让后来者的
    可拦截 hook 按可观察跑，异常就被吞了。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.INTERCEPTABLE)
    with pytest.raises(ContractViolation):
        center.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)

    boom = RuntimeError('artifact policy blew up')

    async def explodes(**_: object) -> HookResult:
        raise boom

    center.register('artifact:created', explodes)
    with pytest.raises(RuntimeError) as excinfo:
        await center.trigger('artifact:created', artifact='raw')
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
        await center.trigger(' artifact:created', artifact='raw')
    assert calls == []


async def test_extension_junction_names_are_case_sensitive():
    """名字计入公开 API，折叠大小写等于让两种拼法映射到同一个切点。"""
    center = HooksCenter()
    center.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    with pytest.raises(ContractViolation):
        await center.trigger('artifact:Created', artifact='raw')


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
        await center.trigger('turn_start', turn=1)
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
        await center.trigger('artifact:created', artifact='raw')


def test_extension_junctions_are_not_shared_between_centers():
    """声明挂在实例上而非模块全局：两个 Runtime 各有一个 HooksCenter，串台会让一个能力的切点
    在另一个 Runtime 里凭空存在。"""
    declaring = HooksCenter()
    declaring.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        HooksCenter().register('artifact:created', cb)
