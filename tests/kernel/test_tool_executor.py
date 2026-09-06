"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而参数非法那条路要读 `tool.parameters`，反射按字符串
取标注的缺陷在这里全绿、在每个真实工具上必炸。
"""

from __future__ import annotations

from typing import Any

import pytest

from syndica.kernel.context import ArtifactRef, Context, ContextBlock, ToolCall, TrustLevel
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    SyndicaError,
    ToolFatalError,
)
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ContextOverflowError
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import ToolExecutor


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Boom(Tool):
    async def execute(self) -> ToolResult:
        """总是抛非致命异常。"""
        raise RuntimeError('kaboom')


class _Fatal(Tool):
    async def execute(self) -> ToolResult:
        """总是抛致命异常。"""
        raise ToolFatalError(
            expected='目标可写',
            received='路径不存在且父目录不可写',
            next_action='换一个可写路径',
        )


class _Raising(Tool):
    """抛出构造时给定的异常。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def execute(self) -> ToolResult:
        """总是抛出预置的异常。"""
        raise self._error


class _Recorder(Tool):
    """记下自己收到的入参，用来区分「工具真的跑了」与「结果被替换掉了」。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, text: str = '') -> ToolResult:
        """回显 text。"""
        self.calls.append({'text': text})
        return ToolResult(output=text)


class _Producer(Tool):
    """交出一个测试自己持有的 ToolResult，用来检查 block 有没有与它共享容器。"""

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    async def execute(self) -> ToolResult:
        """交出预置的结果。"""
        return self.result


_PROPAGATING: list[SyndicaError] = [
    ToolFatalError(expected='目标可写', received='路径不存在', next_action='换一个可写路径'),
    ConfigError(expected='schema 含 parameters', received='缺 parameters', next_action='补上它'),
    ContractViolation(expected='Store 返回 dict', received='返回 None', next_action='修实现'),
    LLMResponseError(expected='响应含 content', received='content 为空', next_action='换个模型'),
    ContextOverflowError(expected='请求不超窗', received='超出 8k', next_action='先压缩上下文'),
]


def _executor() -> tuple[ToolExecutor, HooksCenter]:
    center = HooksCenter()
    tools: dict[str, Tool] = {'Echo': _Echo(), 'Boom': _Boom(), 'Fatal': _Fatal()}
    return ToolExecutor(tools=tools, hooks=center), center


def _artifact(ref_id: str = 'a1') -> ArtifactRef:
    return ArtifactRef(
        id=ref_id,
        path='out.md',
        kind='doc',
        mime='text/markdown',
        version='v1',
        producer='writer',
    )


def _text(block: ContextBlock) -> str:
    content = block.content
    assert isinstance(content, str)
    return content


async def test_result_becomes_tool_block_with_matching_id():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.role == 'tool'
    assert block.tool_call_id == 'c1'
    assert block.content == 'hi'


async def test_blocks_are_appended_after_the_existing_trajectory():
    executor, _ = _executor()
    ctx = Context()
    ctx.append(ContextBlock(role='assistant', content='calling'))
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('assistant', 'calling'),
        ('tool', 'hi'),
    ]


async def test_an_empty_batch_appends_nothing():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [])
    assert ctx.current_traj == []


async def test_each_id_is_paired_with_its_own_result():
    """id 取错、固定成第一个、或结果顺序反了，下一轮请求就把结果配到别的调用上。"""
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(
        ctx,
        [
            ToolCall(id='c1', name='Echo', arguments='{"text":"first"}'),
            ToolCall(id='c2', name='Echo', arguments='{"text":"second"}'),
        ],
    )
    assert [(block.tool_call_id, block.content) for block in ctx.current_traj] == [
        ('c1', 'first'),
        ('c2', 'second'),
    ]


async def test_a_list_output_reaches_the_block_unchanged():
    """`ToolResult.output` 是 `str | list[Any]`；顺手 str() 一下会把多模态内容压成一行字面量。"""
    produced = ToolResult(output=['fetched', {'kind': 'image'}])
    executor = ToolExecutor(tools={'Produce': _Producer(produced)}, hooks=HooksCenter())
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Produce', arguments='{}')])
    assert ctx.current_traj[-1].content == ['fetched', {'kind': 'image'}]


async def test_the_block_copies_the_results_artifacts_instead_of_sharing_the_list():
    artifact = _artifact()
    produced = ToolResult(output='ok', artifacts=[artifact])
    executor = ToolExecutor(tools={'Produce': _Producer(produced)}, hooks=HooksCenter())
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Produce', arguments='{}')])
    block = ctx.current_traj[-1]
    assert block.artifacts == [artifact]
    block.artifacts.append(_artifact('a2'))
    assert produced.artifacts == [artifact]


async def test_result_meta_reaches_extra_and_never_the_content():
    """`output` 给模型、`meta` 给工作流代码，两者不得合并（Task 11）。block 会被持久化并活过这个
    ToolResult，所以拷一份而不是共享。"""
    produced = ToolResult(output='for model', meta={'rows': 3})
    executor = ToolExecutor(tools={'Produce': _Producer(produced)}, hooks=HooksCenter())
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Produce', arguments='{}')])
    block = ctx.current_traj[-1]
    assert block.content == 'for model'
    assert block.extra == {'tool_meta': {'rows': 3}}
    block.extra['tool_meta']['rows'] = 99
    assert produced.meta == {'rows': 3}


async def test_a_result_without_meta_leaves_extra_empty():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].extra == {}


async def test_a_trusted_result_leaves_the_block_trusted():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].trust is TrustLevel.TRUSTED


async def test_untrusted_result_marks_the_tool_block():
    class _External(Tool):
        async def execute(self) -> ToolResult:
            """返回外部内容。"""
            return ToolResult(output='<html>', trust=TrustLevel.UNTRUSTED)

    executor = ToolExecutor(tools={'External': _External()}, hooks=HooksCenter())
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='External', arguments='{}')])
    assert ctx.current_traj[-1].trust is TrustLevel.UNTRUSTED


async def test_unknown_tool_yields_actionable_tool_block():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Nope', arguments='{}')])
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    content = _text(block)
    assert 'Nope' in content
    # 按 dict 顺序交出会让同一状态下的这条消息随宿主注册次序变化，而它会进此后每一次请求的字节
    # 前缀（spec §5.2）。断言渲染后的整份列表，一并钉住「一个都不能少」。
    assert "['Boom', 'Echo', 'Fatal']" in content


async def test_malformed_arguments_yield_actionable_tool_block():
    """模型经常产出非法 JSON。必须给它足以自我纠正的信息，而不是静默失败。"""
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Echo', arguments='{not json')])
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    content = _text(block)
    assert '{not json' in content
    assert 'text' in content


@pytest.mark.parametrize(
    'arguments', ['[1,2]', '"hi"', 'null', '3'], ids=['array', 'string', 'null', 'number']
)
async def test_arguments_that_parse_to_a_non_object_yield_actionable_tool_block(arguments):
    """`**args` 只接受 mapping。让它撞 TypeError，消息会去怪工具，而错在模型给的形状。"""
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Echo', arguments=arguments)])
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    content = _text(block)
    assert arguments in content
    assert 'text' in content


@pytest.mark.parametrize('arguments', ['', '   ', '{}'], ids=['empty', 'blank', 'empty-object'])
async def test_absent_and_empty_object_arguments_both_mean_no_arguments(arguments):
    """模型对「无参数」有三种写法。把空串交给 json.loads 会让它们分道扬镳。"""
    recorder = _Recorder()
    executor = ToolExecutor(tools={'Record': recorder}, hooks=HooksCenter())
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Record', arguments=arguments)])
    assert recorder.calls == [{'text': ''}]
    assert ctx.current_traj[-1].content == ''


async def test_missing_required_arguments_are_reported_not_raised():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Echo', arguments='{}')])
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    assert 'TypeError' in _text(block)


async def test_non_fatal_tool_exception_is_reported_not_raised():
    executor, _ = _executor()
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Boom', arguments='{}')])
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    content = _text(block)
    assert 'kaboom' in content
    assert 'RuntimeError' in content


async def test_fatal_tool_error_propagates_to_agent_loop():
    """spec §10：致命错误必须上抛，Agent 需要知道这条路根本不通。

    转成 output 会让模型以为「再试一次也许能行」，于是它会一直试。
    """
    executor, _ = _executor()
    ctx = Context()
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Fatal', arguments='{}')])


@pytest.mark.parametrize(
    'error', _PROPAGATING, ids=[type(error).__name__ for error in _PROPAGATING]
)
async def test_every_syndica_error_from_a_tool_propagates_instead_of_reaching_the_model(error):
    """三件套是写给程序员或运维的，模型执行不了它的 next_action。而 ConfigError /
    ContractViolation / LLMResponseError / ContextOverflowError 都是 ToolFatalError 的**兄弟**
    而非子类：按 ToolFatalError 划界会把它们全洗进模型上下文。"""
    executor = ToolExecutor(tools={'Raise': _Raising(error)}, hooks=HooksCenter())
    ctx = Context()
    with pytest.raises(SyndicaError) as excinfo:
        await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Raise', arguments='{}')])
    assert excinfo.value is error


async def test_every_call_gets_exactly_one_block():
    """工具调用与结果必须配对，否则下一轮请求直接非法。"""
    executor, _ = _executor()
    ctx = Context()
    calls = [
        ToolCall(id='c1', name='Echo', arguments='{"text":"a"}'),
        ToolCall(id='c2', name='Nope', arguments='{}'),
        ToolCall(id='c3', name='Boom', arguments='{}'),
    ]
    await executor.execute_tool_calls(ctx, calls)
    ids = [block.tool_call_id for block in ctx.current_traj if block.role == 'tool']
    assert ids == ['c1', 'c2', 'c3']


def _aborting_batch() -> list[ToolCall]:
    """两个未执行的调用，顺序断言才区分得出「原序」与「倒序」。"""
    return [
        ToolCall(id='c1', name='Echo', arguments='{"text":"a"}'),
        ToolCall(id='c2', name='Fatal', arguments='{}'),
        ToolCall(id='c3', name='Echo', arguments='{"text":"fabricated"}'),
        ToolCall(id='c4', name='Echo', arguments='{"text":"also-fabricated"}'),
    ]


async def test_a_propagating_failure_still_leaves_one_block_per_call_in_order():
    """上抛前不补齐配对，留下的就是「assistant 声明了 4 个调用、后面只有 1 条结果」——正是这条
    不变式要防的非法状态，而轨迹会被持久化下去。"""
    executor, _ = _executor()
    ctx = Context()
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, _aborting_batch())
    blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert [block.tool_call_id for block in blocks] == ['c1', 'c2', 'c3', 'c4']
    assert blocks[0].content == 'a'


async def test_a_call_that_never_ran_says_so_instead_of_carrying_a_result():
    executor, _ = _executor()
    ctx = Context()
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, _aborting_batch())
    blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert len(blocks) == 4
    for block in blocks[2:]:
        never_ran = _text(block)
        assert 'fabricated' not in never_ran
        assert 'c2' in never_ran and 'Fatal' in never_ran


async def test_the_aborted_calls_block_does_not_invite_a_retry():
    """非致命失败那条消息说的是「调整参数后重试」。把它复用到上抛路径上，模型就会对一条已经判定
    为不通的路反复重试——正是 spec §10 要避免的。"""
    executor, _ = _executor()
    ctx = Context()
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, _aborting_batch())
    aborted = _text([block for block in ctx.current_traj if block.role == 'tool'][1])
    assert 'ToolFatalError' in aborted
    assert 'c2' in aborted
    assert '重试' not in aborted


async def test_hooks_do_not_run_for_a_call_that_never_ran():
    executor, center = _executor()
    pre_seen: list[str] = []
    post_seen: list[str] = []

    async def record_pre(**kwargs: Any) -> HookResult:
        pre_seen.append(kwargs['tool_call'].id)
        return HookResult()

    async def record_post(**kwargs: Any) -> HookResult:
        post_seen.append(kwargs['tool_call'].id)
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, record_pre)
    center.register(Junction.POST_TOOL_USE, record_post)
    ctx = Context()
    calls = [
        ToolCall(id='c1', name='Echo', arguments='{"text":"a"}'),
        ToolCall(id='c2', name='Fatal', arguments='{}'),
        ToolCall(id='c3', name='Echo', arguments='{"text":"c"}'),
    ]
    with pytest.raises(ToolFatalError):
        await executor.execute_tool_calls(ctx, calls)
    assert pre_seen == ['c1', 'c2']
    assert post_seen == ['c1']


async def test_an_interceptable_hook_that_raises_leaves_the_pairing_intact():
    """可拦截 hook 抛出的异常按设计上抛（Task 9）；配对的合法性与异常从哪来无关。"""
    executor, center = _executor()
    boom = RuntimeError('policy check blew up')

    async def explodes(**kwargs: Any) -> HookResult:
        if kwargs['tool_call'].id == 'c2':
            raise boom
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, explodes)
    ctx = Context()
    calls = [
        ToolCall(id='c1', name='Echo', arguments='{"text":"a"}'),
        ToolCall(id='c2', name='Echo', arguments='{"text":"b"}'),
        ToolCall(id='c3', name='Echo', arguments='{"text":"c"}'),
    ]
    with pytest.raises(RuntimeError) as excinfo:
        await executor.execute_tool_calls(ctx, calls)
    assert excinfo.value is boom
    blocks = [block for block in ctx.current_traj if block.role == 'tool']
    assert [block.tool_call_id for block in blocks] == ['c1', 'c2', 'c3']
    assert blocks[0].content == 'a'


async def test_pre_tool_use_receives_the_call_the_tool_and_the_parsed_args():
    """这三个 kwarg 名是 hook 作者写代码的依据，计入公开 API。"""
    executor, center = _executor()
    seen: list[dict[str, Any]] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, record)
    ctx = Context()
    call = ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')
    await executor.execute_tool_calls(ctx, [call])
    assert len(seen) == 1
    assert sorted(seen[0]) == ['args', 'tool', 'tool_call']
    assert seen[0]['tool_call'] is call
    assert seen[0]['args'] == {'text': 'hi'}
    assert isinstance(seen[0]['tool'], _Echo)


async def test_a_pre_tool_use_hook_rewrites_the_args_the_tool_receives():
    """中间件的全部意义：改写不落到工具身上，等于这个 hook 没跑。"""
    recorder = _Recorder()
    center = HooksCenter()
    executor = ToolExecutor(tools={'Record': recorder}, hooks=center)

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'args': {'text': 'rewritten'}})

    center.register(Junction.PRE_TOOL_USE, rewrite)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Record', arguments='{"text":"original"}')]
    )
    assert recorder.calls == [{'text': 'rewritten'}]
    assert ctx.current_traj[-1].content == 'rewritten'


async def test_a_pre_tool_use_hook_can_rewrite_the_args_to_an_empty_dict():
    """空 dict 与「没改写」是两件事。按真值判断会让「把参数全清掉」这条改写静默失效。"""
    recorder = _Recorder()
    center = HooksCenter()
    executor = ToolExecutor(tools={'Record': recorder}, hooks=center)

    async def strip_all(**_: Any) -> HookResult:
        return HookResult(modified={'args': {}})

    center.register(Junction.PRE_TOOL_USE, strip_all)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Record', arguments='{"text":"original"}')]
    )
    assert recorder.calls == [{'text': ''}]


@pytest.mark.parametrize(
    'rewritten',
    ['{"text":"hi"}', ['text'], None, 42],
    ids=['str', 'list', 'none', 'int'],
)
async def test_a_pre_tool_use_hook_rewriting_args_to_a_non_dict_is_rejected(rewritten):
    """`tool.execute(**args)` 撞 TypeError 时消息会去怪工具，而错在 hook 的返回形状。"""
    executor, center = _executor()

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'args': rewritten})

    center.register(Junction.PRE_TOOL_USE, rewrite)
    ctx = Context()
    with pytest.raises(ContractViolation) as excinfo:
        await executor.execute_tool_calls(
            ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
        )
    assert Junction.PRE_TOOL_USE.value in excinfo.value.expected
    assert [block.tool_call_id for block in ctx.current_traj] == ['c1']


async def test_pre_tool_use_skip_substitutes_result_and_the_tool_never_runs():
    recorder = _Recorder()
    center = HooksCenter()
    executor = ToolExecutor(tools={'Record': recorder}, hooks=center)

    async def block_it(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='substituted'))

    center.register(Junction.PRE_TOOL_USE, block_it)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Record', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    assert block.content == 'substituted'
    assert recorder.calls == []


async def test_a_substituted_result_keeps_its_untrusted_mark_and_artifacts():
    """替代结果同样可以来自外部（缓存命中、旁路读取）。只取 `output` 重建一个 ToolResult 会把
    不可信标记与产物一起丢掉，而标记必须随衍生内容存活（spec §12.2）。"""
    artifact = _artifact()
    executor, center = _executor()

    async def serve_from_cache(**_: Any) -> HookResult:
        return HookResult(
            skip=True,
            result=ToolResult(
                output='<html>',
                meta={'cached': True},
                artifacts=[artifact],
                trust=TrustLevel.UNTRUSTED,
            ),
        )

    center.register(Junction.PRE_TOOL_USE, serve_from_cache)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.content == '<html>'
    assert block.trust is TrustLevel.UNTRUSTED
    assert block.artifacts == [artifact]
    assert block.extra == {'tool_meta': {'cached': True}}


async def test_a_rewritten_result_keeps_its_untrusted_mark_and_artifacts():
    artifact = _artifact()
    executor, center = _executor()

    async def wrap_with_external_content(**_: Any) -> HookResult:
        return HookResult(
            modified={
                'result': ToolResult(
                    output='<html>',
                    meta={'fetched': True},
                    artifacts=[artifact],
                    trust=TrustLevel.UNTRUSTED,
                )
            }
        )

    center.register(Junction.POST_TOOL_USE, wrap_with_external_content)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.trust is TrustLevel.UNTRUSTED
    assert block.artifacts == [artifact]
    assert block.extra == {'tool_meta': {'fetched': True}}


async def test_post_tool_use_does_not_run_when_pre_tool_use_skips():
    executor, center = _executor()
    post_seen: list[str] = []

    async def block_it(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='substituted'))

    async def record_post(**kwargs: Any) -> HookResult:
        post_seen.append(kwargs['tool_call'].id)
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, block_it)
    center.register(Junction.POST_TOOL_USE, record_post)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert post_seen == []


async def test_a_skip_result_that_is_not_a_tool_result_becomes_the_output():
    """拒绝类 hook 最自然的写法是 `result='denied: ...'`；`result` 是通用的 Any 槽位。"""
    executor, center = _executor()

    async def deny(**_: Any) -> HookResult:
        return HookResult(skip=True, result='denied by policy')

    center.register(Junction.PRE_TOOL_USE, deny)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].content == 'denied by policy'


async def test_a_skip_without_a_result_reports_that_nothing_ran():
    """`result` 默认就是 None，即「没有给出替代结果」。`str(None)` 会把 'None' 塞进模型上下文，
    读起来像工具真返回了这个字符串。"""
    executor, center = _executor()

    async def block_it(**_: Any) -> HookResult:
        return HookResult(skip=True)

    center.register(Junction.PRE_TOOL_USE, block_it)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    block = ctx.current_traj[-1]
    assert block.tool_call_id == 'c1'
    content = _text(block)
    assert content != 'None'
    assert 'Echo' in content and 'c1' in content


async def test_post_tool_use_receives_the_result_and_the_args_the_tool_actually_got():
    executor, center = _executor()
    seen: list[dict[str, Any]] = []

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'args': {'text': 'rewritten'}})

    async def record(**kwargs: Any) -> HookResult:
        seen.append(dict(kwargs))
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, rewrite)
    center.register(Junction.POST_TOOL_USE, record)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"original"}')]
    )
    assert len(seen) == 1
    assert sorted(seen[0]) == ['args', 'result', 'tool', 'tool_call']
    assert seen[0]['args'] == {'text': 'rewritten'}
    assert seen[0]['result'].output == 'rewritten'


async def test_post_tool_use_can_rewrite_result():
    executor, center = _executor()

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'result': ToolResult(output='rewritten')})

    center.register(Junction.POST_TOOL_USE, rewrite)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].content == 'rewritten'


async def test_post_tool_use_returning_no_opinion_leaves_the_result_alone():
    executor, center = _executor()

    async def observe(**_: Any) -> HookResult:
        return HookResult()

    center.register(Junction.POST_TOOL_USE, observe)
    ctx = Context()
    await executor.execute_tool_calls(
        ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
    )
    assert ctx.current_traj[-1].content == 'hi'


@pytest.mark.parametrize('rewritten', ['rewritten', None, 42], ids=['str', 'none', 'int'])
async def test_a_post_tool_use_hook_rewriting_result_to_a_non_result_is_rejected(rewritten):
    """静默丢弃改写是三条路里最坏的一条：作者以为自己改了，模型看到的还是原结果。"""
    executor, center = _executor()

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'result': rewritten})

    center.register(Junction.POST_TOOL_USE, rewrite)
    ctx = Context()
    with pytest.raises(ContractViolation) as excinfo:
        await executor.execute_tool_calls(
            ctx, [ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
        )
    assert Junction.POST_TOOL_USE.value in excinfo.value.expected
    assert [block.tool_call_id for block in ctx.current_traj] == ['c1']


async def test_hooks_do_not_run_for_an_unknown_tool():
    """PRE_TOOL_USE 的 `tool` kwarg 没有可填的值——工具根本不存在。"""
    executor, center = _executor()
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['tool_call'].id)
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, record)
    center.register(Junction.POST_TOOL_USE, record)
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Nope', arguments='{}')])
    assert seen == []
    assert [block.tool_call_id for block in ctx.current_traj] == ['c1']


async def test_hooks_do_not_run_for_malformed_arguments():
    """PRE_TOOL_USE 的 `args` kwarg 没有可填的值——参数没解析出来。"""
    executor, center = _executor()
    seen: list[str] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['tool_call'].id)
        return HookResult()

    center.register(Junction.PRE_TOOL_USE, record)
    center.register(Junction.POST_TOOL_USE, record)
    ctx = Context()
    await executor.execute_tool_calls(ctx, [ToolCall(id='c1', name='Echo', arguments='{not json')])
    assert seen == []
    assert [block.tool_call_id for block in ctx.current_traj] == ['c1']
