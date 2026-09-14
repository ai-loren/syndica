"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentStatus, ExitMode
from syndica.kernel.context import ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.danger import AllowAll, DangerPolicy
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools import Delegate, Fork
from syndica.kernel.waiting import WaitState

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 3.0
_THREE_PART: tuple[str, ...] = ('期望: ', ' 实际: ', ' 下一步: ')
# 一条命中默认规则表里 shell.privilege_escalation 的命令。闸有没有被 §12.2 第 3 条那道升级接走，
# 只有「它到底执行了没有」答得出。
_DANGEROUS = '{"command":"sudo rm -rf /"}'


class _Tainted(Tool):
    """产出不可信内容的工具：外部抓来的东西就是这个样子。"""

    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """回显 text，并标记为不可信。"""
        return ToolResult(output=text, trust=TrustLevel.UNTRUSTED)


class _Runner(Tool):
    """记录它真的执行过什么。闸拦下了没有，只有这份记录答得出。"""

    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.executed.append(command)
        return ToolResult(output='ran')


class _Block(Tool):
    """进入后挂起，直到测试放行。"""

    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self) -> ToolResult:
        """挂住这一轮。"""
        self.entered.set()
        await self.release.wait()
        return ToolResult(output='released')


def _worker(
    script: list[ScriptedTurn],
    tools: dict[str, Tool] | None = None,
    *,
    name: str = 'Worker',
    max_turns: int | None = 50,
    policy: DangerPolicy | None = None,
) -> AgentFactory:
    """一个角色的工厂。

    **`ScriptedLLM` 建在 `build` 之外，因此同一个工厂造出来的 Agent 共用一个脚本游标。** 分叉出
    来的 Agent 拿的是源 Agent 的工厂（这就是「同类型」的含义），脚本每次从头重放的话，分身会把
    源 Agent 刚做过的那次 `Fork` 原样再做一遍——一次无限递归的分叉，表现为整条用例挂死。
    """
    llm = ScriptedLLM(script)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name=name,
                llm=llm,
                tools={} if tools is None else tools,
                system_prompt='SP',
                max_turns=max_turns,
                danger_policy=policy,
            ),
            hooks=hooks,
            events=events,
        )

    return build


def _orchestrator(
    script: list[ScriptedTurn],
    runtime: Runtime,
    extra: dict[str, Tool] | None = None,
    *,
    policy: DangerPolicy | None = None,
) -> AgentFactory:
    """装上编排工具的 Agent 工厂。

    `agent_id` 在 `BaseAgent.__init__` 里才生成，所以工具只能在建好实例之后挂进那张**活表**
    （`AgentOptions.tools` 刻意不换成副本）。派活因此真的从一次工具调用内部发起——这是
    `wait_in_turn` 唯一能被打断的位置。脚本共用一个游标，理由同 `_worker`。
    """
    llm = ScriptedLLM(script)

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        tools: dict[str, Tool] = dict(extra or {})
        agent = BaseAgent(
            AgentOptions(
                name='Boss',
                llm=llm,
                tools=tools,
                system_prompt='SP',
                danger_policy=policy,
            ),
            hooks=hooks,
            events=events,
        )
        tools['Delegate'] = Delegate(runtime, agent.agent_id)
        tools['Fork'] = Fork(runtime, agent.agent_id)
        return agent

    return build


async def _idle_parent(runtime: Runtime) -> str:
    """一个已经跑完一个周期、停在 ZOMBIE 的父 Agent：直接调编排工具时用它当发起方。"""
    parent = await runtime.spawn(_worker([ScriptedTurn(content='parent idle')]), query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    return parent


def _call(name: str, arguments: str, call_id: str = 'c1') -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def _tool_blocks(runtime: Runtime, agent_id: str) -> list[ContextBlock]:
    agent = runtime._table.get(agent_id)
    assert agent is not None
    return [block for block in agent.context.current_traj if block.role == 'tool']


def _traj(runtime: Runtime, agent_id: str) -> list[ContextBlock]:
    agent = runtime._table.get(agent_id)
    assert agent is not None
    return agent.context.current_traj


# ---------------------------------------------------------------- Delegate


async def test_delegate_spawns_a_child_waits_for_it_and_brings_back_its_final_answer():
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='the child answer')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='do the thing'),
        timeout=_TIMEOUT,
    )

    assert 'the child answer' in result.output
    assert result.meta['stop_reason'] == 'completed'
    child_id = result.meta['agent_id']
    info = await runtime.find(child_id)
    assert info is not None and info.parent_id == parent
    await runtime.shutdown()


async def test_the_child_id_reaches_the_model_and_not_only_the_workflow_code():
    """`meta` 不进模型上下文（它落在 block 的 `extra` 里），所以只放在那里等于模型拿不到它。

    拿不到 id 的模型既不能让这个 ZOMBIE 带着上下文继续干，也不能停掉它——那 ZOMBIE 就是纯泄漏。
    """
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='done')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert isinstance(result.output, str)
    assert result.meta['agent_id'] in result.output
    await runtime.shutdown()


async def test_a_delegated_child_stays_addressable_so_the_parent_can_keep_talking_to_it():
    """spec §5.6：已有 Agent 能继续同一工作时用 SendMessage，不要新建。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='first')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='first job'), timeout=_TIMEOUT
    )

    info = await runtime.find(result.meta['agent_id'])
    assert info is not None and info.status is AgentStatus.ZOMBIE
    await runtime.shutdown()


async def test_delegating_to_an_unknown_role_tells_the_model_which_roles_exist():
    """未知角色是模型改得动的错误，因此进 output 而不是上抛。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='x')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Nope', query='x'), timeout=_TIMEOUT
    )

    assert isinstance(result.output, str)
    assert 'Worker' in result.output
    assert 'Nope' in result.output
    assert all(part in result.output for part in _THREE_PART)
    await runtime.shutdown()


async def test_delegate_reports_a_child_that_ran_out_of_budget_instead_of_pretending_it_answered():
    runtime = Runtime(
        agents={'Worker': _worker([ScriptedTurn(content='x')], max_turns=0)},
    )
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert result.meta['stop_reason'] == 'max_turns'
    assert isinstance(result.output, str)
    assert all(part in result.output for part in _THREE_PART)
    assert result.meta['agent_id'] in result.output
    await runtime.shutdown()


async def test_a_child_that_left_text_before_running_out_of_budget_is_still_reported_unfinished():
    """有文字不等于有结论：一个撞上预算上限的子 Agent 留下的最后一段文字是半句话。

    把它当成答案交回去，父 Agent 会在一件没做完的工作上继续往下走，而「它做完了没有」这件事
    此后再没有第二处证据。
    """
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [
                    ScriptedTurn(
                        content='partial thinking',
                        tool_calls=[ToolCall('c1', 'Tainted', '{"text":"x"}')],
                    )
                ],
                {'Tainted': _Tainted()},
                max_turns=1,
            )
        }
    )
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert result.meta['stop_reason'] == 'max_turns'
    assert all(part in str(result.output) for part in _THREE_PART)
    assert 'partial thinking' not in str(result.output)
    await runtime.shutdown()


async def test_killing_the_child_releases_a_parent_that_is_waiting_on_it():
    """这次等待没有墙钟上限（登记 D-47），而「被等的那一方在本 Runtime 手里」是那条裁决的全部
    依据——所以那三条出路里至少要有一条被真的走过一遍，否则依据就只是一句断言。

    硬终止不产生 `stop_reason`，于是那张终局表里 `None` 那一格也在这条路上被走到。
    """
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='never reached')], {'Block': blocker}
            )
        }
    )
    parent = await _idle_parent(runtime)
    delegating = asyncio.ensure_future(
        Delegate(runtime, parent).execute(agent='Worker', query='slow job')
    )
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    child_id = next(iter(set(runtime._slots) - {parent}))

    await runtime.kill(child_id)
    result = await asyncio.wait_for(delegating, timeout=_TIMEOUT)

    assert result.meta == {'agent_id': child_id, 'stop_reason': None}
    assert all(part in str(result.output) for part in _THREE_PART)
    assert '硬终止' in str(result.output)
    await runtime.shutdown()


async def test_a_child_that_died_of_an_exception_is_reported_as_having_failed():
    runtime = Runtime(
        agents={'Worker': _worker([ScriptedTurn(raises=RuntimeError('provider is down'))])}
    )
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert result.meta['stop_reason'] == 'error'
    assert '错误' in str(result.output)
    await runtime.shutdown()


async def test_a_child_that_touched_untrusted_content_hands_an_untrusted_result_to_the_parent():
    """标记必须随衍生内容传递：子 Agent 的结论是它读过的东西的衍生物（spec §12.2）。"""
    child_script = [
        _call('Tainted', '{"text":"web"}'),
        ScriptedTurn(content='summarised from the web'),
    ]
    runtime = Runtime(agents={'Worker': _worker(child_script, {'Tainted': _Tainted()})})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='research'), timeout=_TIMEOUT
    )

    assert result.trust is TrustLevel.UNTRUSTED
    await runtime.shutdown()


async def test_a_clean_child_hands_back_a_trusted_result():
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='clean')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert result.trust is TrustLevel.TRUSTED
    await runtime.shutdown()


async def test_three_agents_delegating_at_once_each_get_their_own_child_and_their_own_answer():
    """按 agent_id 索引的东西都要证明互不串线；基数 3，答案既非升序亦非降序。"""
    runtime = Runtime(
        agents={
            'Beta': _worker([ScriptedTurn(content='beta answer')], name='Beta'),
            'Alpha': _worker([ScriptedTurn(content='alpha answer')], name='Alpha'),
            'Gamma': _worker([ScriptedTurn(content='gamma answer')], name='Gamma'),
        }
    )
    parents = [await _idle_parent(runtime) for _ in range(3)]

    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                Delegate(runtime, parent).execute(agent=role, query=f'job for {role}')
                for parent, role in zip(parents, ('Beta', 'Alpha', 'Gamma'), strict=True)
            )
        ),
        timeout=_TIMEOUT,
    )

    assert [str(result.output).split('\n')[-1] for result in results] == [
        'beta answer',
        'alpha answer',
        'gamma answer',
    ]
    children = [result.meta['agent_id'] for result in results]
    assert len(set(children)) == 3
    for parent, child in zip(parents, children, strict=True):
        info = await runtime.find(child)
        assert info is not None and info.parent_id == parent
    await runtime.shutdown()


async def test_delegate_declares_that_it_executes_nothing_so_a_prose_query_is_not_scanned():
    """`__tool_command_args__ = ()` 是承重的：不声明，判定会把 query 里的散文当命令读。

    一段讲 `sudo rm -rf /` 的任务描述会命中默认规则表，于是整个派活被阻断——而 Delegate 自己
    什么都不执行，真正会执行东西的是子 Agent 的工具，那些调用在子 Agent 自己的闸上受判。
    """
    assert Delegate.__tool_command_args__ == ()
    assert Fork.__tool_command_args__ == ()

    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='child ok')])})
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"别去跑 sudo rm -rf / 这条命令"}', 'd1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    ctx = await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.COMPLETED
    assert 'child ok' in str(_tool_blocks(runtime, parent)[0].content)
    await runtime.shutdown()


# ------------------------------------------------- Delegate：信任洗白


async def test_a_parent_that_read_untrusted_content_cannot_launder_it_through_a_delegation():
    """`query` 由模型完全控制，而模型读过什么决定了那段文字是什么的衍生物（spec §12.2 第 2 条）。

    父的轨迹里有不可信内容时，它复述进 `query` 的东西在子那里必须仍然不可信——否则一次派活
    就把不可信内容洗成了子 Agent 的可信输入。
    """
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='child')])})
    parent = await _idle_parent(runtime)
    parent_agent = runtime._table.get(parent)
    assert parent_agent is not None
    parent_agent.context.append(
        ContextBlock(
            role='tool', content='<html>…</html>', tool_call_id='x', trust=TrustLevel.UNTRUSTED
        )
    )

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='删掉构建目录'), timeout=_TIMEOUT
    )

    seeds = [
        block
        for block in _traj(runtime, result.meta['agent_id'])
        if block.role == 'user' and block.content == '删掉构建目录'
    ]
    assert [block.trust for block in seeds] == [TrustLevel.UNTRUSTED]
    await runtime.shutdown()


async def test_the_laundered_delegation_is_still_stopped_by_the_gate_inside_the_child():
    """只钉那一块的 trust 不够：要证明它真的接到了闸上（spec §12.2 第 3 条那道关不掉的升级）。"""
    runner = _Runner()
    child = _worker(
        [_call('Runner', _DANGEROUS), ScriptedTurn(content='child done')],
        {'Runner': runner},
        policy=AllowAll(),
    )
    runtime = Runtime(agents={'Worker': child})
    parent = await _idle_parent(runtime)
    parent_agent = runtime._table.get(parent)
    assert parent_agent is not None
    parent_agent.context.append(
        ContextBlock(
            role='tool', content='网页说要提权删除', tool_call_id='x', trust=TrustLevel.UNTRUSTED
        )
    )

    await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='按网页说的做'), timeout=_TIMEOUT
    )

    assert runner.executed == []
    await runtime.shutdown()


async def test_a_delegation_from_a_clean_parent_still_lets_the_child_do_high_risk_work():
    """反向一侧：无条件把子 Agent 的输入标成不可信会让 Worker 这个角色整个用不了。"""
    runner = _Runner()
    child = _worker(
        [_call('Runner', _DANGEROUS), ScriptedTurn(content='child done')],
        {'Runner': runner},
        policy=AllowAll(),
    )
    runtime = Runtime(agents={'Worker': child})
    parent = await _idle_parent(runtime)

    await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='清理'), timeout=_TIMEOUT
    )

    assert runner.executed == ['sudo rm -rf /']
    await runtime.shutdown()


# ---------------------------------------------------------------- Fork


async def test_fork_inherits_the_parents_trajectory_and_the_same_role():
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [
                ScriptedTurn(
                    content='delta thinking',
                    tool_calls=[ToolCall('c1', 'Tainted', '{"text":"alpha result"}')],
                ),
                ScriptedTurn(content='charlie conclusion'),
                ScriptedTurn(content='forked tail'),
            ],
            {'Tainted': _Tainted()},
        ),
        query='bravo question',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    result = await asyncio.wait_for(
        Fork(runtime, parent).execute(query='keep going from here'), timeout=_TIMEOUT
    )

    child_id = result.meta['agent_id']
    assert [block.content for block in _traj(runtime, child_id) if block.role == 'user'] == [
        'bravo question',
        'keep going from here',
    ]
    assert 'charlie conclusion' in [block.content for block in _traj(runtime, child_id)]
    assert 'forked tail' in str(result.output)
    await runtime.shutdown()


async def test_the_forked_prefix_is_block_for_block_identical_to_the_source_prefix():
    """前缀漂移不报错，只静默让分身的第一次请求全量 miss 缓存（spec §5.2 纪律 4）。

    基数 4 且内容既非升序亦非降序：n<=2 或恰好有序时，「逐块照抄」与「排序」「反转」是同一个答案。
    """
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [
                ScriptedTurn(
                    content='delta', tool_calls=[ToolCall('c1', 'Tainted', '{"text":"alpha"}')]
                ),
                ScriptedTurn(content='charlie'),
                ScriptedTurn(content='tail'),
            ],
            {'Tainted': _Tainted()},
        ),
        query='bravo',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    source = _traj(runtime, parent)
    assert [block.content for block in source] == ['bravo', 'delta', 'alpha', 'charlie']

    result = await asyncio.wait_for(Fork(runtime, parent).execute(query='onward'), timeout=_TIMEOUT)

    forked = _traj(runtime, result.meta['agent_id'])
    assert [
        (block.role, block.content, block.tool_call_id, [call.id for call in block.tool_calls])
        for block in forked[: len(source)]
    ] == [
        (block.role, block.content, block.tool_call_id, [call.id for call in block.tool_calls])
        for block in source
    ]
    await runtime.shutdown()


async def test_the_forked_trajectory_is_a_copy_so_the_twin_cannot_write_into_its_source():
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker([ScriptedTurn(content='source conclusion'), ScriptedTurn(content='twin')]),
        query='original',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = await runtime.spawn(
        _worker([ScriptedTurn(content='twin')]), query='onward', fork_from=parent
    )
    forked = _traj(runtime, child_id)[0]
    forked.content = 'rewritten by the twin'
    forked.extra['smuggled'] = True
    forked.tool_calls.append(ToolCall('forged', 'Nope', '{}'))

    source = _traj(runtime, parent)[0]
    assert source.content == 'original'
    assert source.extra == {}
    assert source.tool_calls == []
    await runtime.shutdown()


async def test_forking_keeps_the_untrusted_marks_that_were_inside_the_copied_prefix():
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [_call('Tainted', '{"text":"from the web"}'), ScriptedTurn(content='conclusion')],
            {'Tainted': _Tainted()},
        ),
        query='go',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = await runtime.spawn(
        _worker([ScriptedTurn(content='twin')]), query='onward', fork_from=parent
    )

    assert [
        block.trust for block in _traj(runtime, child_id) if block.content == 'from the web'
    ] == [TrustLevel.UNTRUSTED]
    await runtime.shutdown()


async def test_the_twin_of_a_tainted_agent_is_still_stopped_by_the_gate():
    """把复制过来的标记接到闸上：只钉块自己的 trust 不够。"""
    runner = _Runner()
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [_call('Tainted', '{"text":"from the web"}'), ScriptedTurn(content='conclusion')],
            {'Tainted': _Tainted()},
            policy=AllowAll(),
        ),
        query='go',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = await runtime.spawn(
        _worker(
            [_call('Runner', _DANGEROUS), ScriptedTurn(content='twin done')],
            {'Runner': runner},
            policy=AllowAll(),
        ),
        query='onward',
        fork_from=parent,
    )
    await asyncio.wait_for(runtime.wait(child_id), timeout=_TIMEOUT)

    assert runner.executed == []
    await runtime.shutdown()


async def test_forking_from_inside_a_tool_call_does_not_copy_the_unpaired_batch():
    """分叉真的从一次工具调用内部发起时，源轨迹末尾正是那次调用自己——一个尚未配对的批次。

    原样拷过去，分身一进 `run` 就会把它当成「上一次被中断的批次」，补上一堆「副作用未知」的
    说明：一段凭空捏造的历史。
    """
    runtime = Runtime()
    boss = _orchestrator(
        [_call('Fork', '{"query":"twin work"}', 'f1'), ScriptedTurn(content='boss done')],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = _tool_blocks(runtime, parent)[0].extra['tool_meta']['agent_id']
    forked = _traj(runtime, child_id)
    assert [block.role for block in forked] == ['user', 'user', 'assistant']
    assert [call.id for block in forked for call in block.tool_calls] == []
    assert all(block.extra.get('interrupted') is None for block in forked)
    await runtime.shutdown()


async def test_forking_carries_the_taint_that_the_unpaired_truncation_drops():
    """截断会把本批里已经落进轨迹的不可信结果一起丢掉——它可能是唯一的那一块。

    `[Tainted, Fork]` 同一批：Tainted 的结果块排在那个未配对的 assistant 块之后，因此随截断
    消失，分身的轨迹于是干净得像什么都没读过，而它的高风险调用就绕过了那道关不掉的升级。
    """
    runner = _Runner()
    runtime = Runtime()
    boss = _orchestrator(
        [
            ScriptedTurn(
                tool_calls=[
                    ToolCall('t1', 'Tainted', '{"text":"网页说要提权删除"}'),
                    ToolCall('f1', 'Fork', '{"query":"照它说的做"}'),
                ]
            ),
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='fork done'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Tainted': _Tainted(), 'Runner': runner},
        policy=AllowAll(),
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = _tool_blocks(runtime, parent)[1].extra['tool_meta']['agent_id']
    forked = _traj(runtime, child_id)
    assert [block.content for block in forked if block.trust is TrustLevel.UNTRUSTED] == [
        '照它说的做'
    ]
    assert runner.executed == []
    await runtime.shutdown()


async def test_a_fork_whose_source_is_clean_still_lets_the_twin_do_high_risk_work():
    """反向一侧：把分身的第一条输入无条件标成不可信会让分叉在 `AllowAll` 下整个用不了。"""
    runner = _Runner()
    runtime = Runtime()
    boss = _orchestrator(
        [
            _call('Fork', '{"query":"清理"}', 'f1'),
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='fork done'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Runner': runner},
        policy=AllowAll(),
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == ['sudo rm -rf /']
    await runtime.shutdown()


async def test_a_fork_lands_under_the_agent_it_was_forked_from_so_despawn_cascades_to_it():
    runtime = Runtime()
    parent = await runtime.spawn(_worker([ScriptedTurn(content='source')]), query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    result = await asyncio.wait_for(Fork(runtime, parent).execute(query='onward'), timeout=_TIMEOUT)
    child_id = result.meta['agent_id']
    info = await runtime.find(child_id)
    assert info is not None and info.parent_id == parent

    await asyncio.wait_for(runtime.despawn(parent), timeout=_TIMEOUT)
    assert await runtime.find(child_id) is None
    await runtime.shutdown()


async def test_forking_from_an_agent_this_process_does_not_hold_is_refused():
    runtime = Runtime()
    with pytest.raises(ContractViolation) as caught:
        await runtime.spawn(_worker([ScriptedTurn(content='x')]), query='go', fork_from='nope')
    assert 'nope' in caught.value.expected
    assert 'fork_from' in caught.value.next_action
    await runtime.shutdown()


async def test_forking_an_agent_whose_construction_this_process_never_saw_is_a_model_facing_error():
    """`factory_of` 答不出来时 `Fork` 没有「同类型」可照着造；这是模型改得动的错误，进 output。"""
    runtime = Runtime()
    result = await asyncio.wait_for(Fork(runtime, 'not-a-real-agent').execute(query='x'), _TIMEOUT)

    assert isinstance(result.output, str)
    assert all(part in result.output for part in _THREE_PART)
    assert 'Delegate' in result.output
    await runtime.shutdown()


# ------------------------------------------------- abort 穿透派活等待


async def test_aborting_a_parent_that_is_waiting_on_a_delegation_does_not_have_to_wait_for_it():
    """中止位只在轮的边界上被读到，而此刻这一轮悬在一次派活等待上——不打断它，一次 `abort`
    要等子 Agent 自己干完才兑现，而那可以是几小时。
    """
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"slow job"}', 'd1'),
            ScriptedTurn(content='never reached'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)

    await runtime.abort(parent)
    ctx = await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    block = _tool_blocks(runtime, parent)[0]
    child_id = block.extra['tool_meta']['agent_id']
    assert child_id in str(block.content)
    info = await runtime.find(child_id)
    assert info is not None
    await runtime.shutdown()


async def test_an_abort_that_landed_before_the_delegation_started_is_not_swallowed_by_it():
    """中止可以在派活之前就到达（同一批里更早的调用、一次还在飞的模型调用）。

    此刻没有令牌可打，所以装上令牌之后的那次复检是这条路唯一的出口；而它**只读不取**——位由轮
    边界消费，在这里取走它，这一轮就不会以 ABORTED 收场，一次 abort 于是静默失效。
    """
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"slow job"}', 'd1'),
            ScriptedTurn(content='never reached'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    aborted: list[str] = []

    async def abort_before_the_tool_runs(**kwargs: object) -> HookResult:
        if not aborted:
            aborted.append('once')
            await runtime.abort(parent)
        return HookResult()

    runtime.hooks.register(Junction.PRE_TOOL_USE, abort_before_the_tool_runs)
    ctx = await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert aborted == ['once']
    # 位没被这次等待取走，所以轮边界照样兑现它。
    assert ctx.stop_reason is StopReason.ABORTED
    # 没有等那个挂住的子 Agent：等了就是挂死在 `runtime.wait(parent)` 上。
    assert '中止' in str(_tool_blocks(runtime, parent)[0].content)
    await runtime.shutdown()


async def test_the_child_of_an_aborted_delegation_keeps_running_and_stays_waitable():
    """打断的是这次等待，不是被等的那一方：`abort(父)` 不是一次落在子 Agent 上的 `kill`。

    子 Agent 因此仍然跑得完、仍然拿得到结论——那正是把它的 agent_id 交给模型的前提。
    """
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"slow job"}', 'd1'),
            ScriptedTurn(content='never reached'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    await runtime.abort(parent)
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    child_id = _tool_blocks(runtime, parent)[0].extra['tool_meta']['agent_id']

    blocker.release.set()
    child_ctx = await asyncio.wait_for(runtime.wait(child_id), timeout=_TIMEOUT)

    assert child_ctx.stop_reason is StopReason.COMPLETED
    assert 'child done' in [block.content for block in child_ctx.current_traj]
    await runtime.shutdown()


async def test_a_delegation_interrupted_by_abort_says_the_child_may_still_be_running():
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"slow job"}', 'd1'),
            ScriptedTurn(content='never reached'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    await runtime.abort(parent)
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    content = str(_tool_blocks(runtime, parent)[0].content)
    assert all(part in content for part in _THREE_PART)
    assert '中止' in content
    await runtime.shutdown()


async def test_a_child_that_finished_is_delivered_even_though_an_abort_already_arrived():
    """已经跑完的那份轨迹此刻丢掉没有任何好处，而中止仍会在轮边界上兑现。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='child answer')])})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='p')]), query='go')
    child = await runtime.spawn(_worker([ScriptedTurn(content='child answer')]), query='x')
    await asyncio.wait_for(runtime.wait(child), timeout=_TIMEOUT)
    slot = runtime._slots[parent]
    slot.control.request_abort()

    result = await asyncio.wait_for(runtime.wait_in_turn(parent, child), timeout=_TIMEOUT)

    assert result.state.value == 'answered'
    assert result.context is not None and result.context.stop_reason is StopReason.COMPLETED
    await runtime.shutdown()


async def test_a_second_in_turn_wait_preempts_the_first_one():
    """一个 Agent 是一份注意力：两个并发的派活等待里必然有一个的结果没人会收。"""
    blocker = _Block()
    runtime = Runtime()
    parent = await _idle_parent(runtime)
    slow = await runtime.spawn(
        _worker([_call('Block', '{}'), ScriptedTurn(content='x')], {'Block': blocker}), query='go'
    )
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    first = asyncio.ensure_future(runtime.wait_in_turn(parent, slow))
    await asyncio.sleep(0)

    second = asyncio.ensure_future(runtime.wait_in_turn(parent, slow))
    outcome = await asyncio.wait_for(first, timeout=_TIMEOUT)

    assert outcome.state.value == 'preempted'
    assert outcome.context is None
    blocker.release.set()
    assert (await asyncio.wait_for(second, timeout=_TIMEOUT)).state.value == 'answered'
    await runtime.shutdown()


async def test_a_preempted_wait_does_not_release_the_slot_the_one_that_preempted_it_is_using():
    """被顶掉的那次等待先返回时，槽里装的是顶掉它的那个令牌，而那次等待还挂着。

    无条件清掉它，`abort` 就再也打断不了还在飞的那一次——而那正是这个槽存在的全部理由。
    """
    blocker = _Block()
    runtime = Runtime()
    parent = await _idle_parent(runtime)
    slow = await runtime.spawn(
        _worker([_call('Block', '{}'), ScriptedTurn(content='x')], {'Block': blocker}), query='go'
    )
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    first = asyncio.ensure_future(runtime.wait_in_turn(parent, slow))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(runtime.wait_in_turn(parent, slow))
    assert (await asyncio.wait_for(first, timeout=_TIMEOUT)).state is WaitState.PREEMPTED

    runtime._slots[parent].control.request_abort()
    delegation = runtime._slots[parent].delegation
    assert delegation is not None
    delegation.interrupt(WaitState.ABORTED)

    assert (await asyncio.wait_for(second, timeout=_TIMEOUT)).state is WaitState.ABORTED
    blocker.release.set()
    await runtime.shutdown()


async def test_waiting_in_turn_on_behalf_of_an_agent_this_process_never_took_over_is_refused():
    runtime = Runtime()
    parent = await _idle_parent(runtime)
    with pytest.raises(ContractViolation) as caught:
        await runtime.wait_in_turn('not-mine', parent)
    assert 'not-mine' in caught.value.expected
    await runtime.shutdown()


async def test_a_delegation_does_not_report_the_parent_as_waiting_for_a_reply():
    """`WAITING` 说的是「这一轮悬在一条匹配的答复上」；等一个子 Agent 干活是这一轮的工作本身。"""
    blocker = _Block()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"slow"}', 'd1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)

    info = await runtime.find(parent)
    assert info is not None and info.status is AgentStatus.RUNNING

    await runtime.pause(parent)
    await runtime.resume(parent)
    resumed = await runtime.find(parent)
    assert resumed is not None and resumed.status is AgentStatus.RUNNING
    blocker.release.set()
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    await runtime.shutdown()


# ------------------------------------------------- 角色表


async def test_registered_agent_names_are_a_snapshot_in_registration_order():
    """基数 3 且既非升序亦非降序：角色表的顺序会被 Delegate 的错误消息逐字消费。"""
    runtime = Runtime(
        agents={
            'Beta': _worker([ScriptedTurn(content='b')]),
            'Alpha': _worker([ScriptedTurn(content='a')]),
            'Gamma': _worker([ScriptedTurn(content='g')]),
        }
    )
    assert runtime.agent_names == ('Beta', 'Alpha', 'Gamma')
    assert isinstance(runtime.agent_names, tuple)
    await runtime.shutdown()


async def test_the_role_table_is_a_snapshot_of_the_mapping_it_was_given():
    declared: dict[str, AgentFactory] = {'Worker': _worker([ScriptedTurn(content='w')])}
    runtime = Runtime(agents=declared)

    declared['Sneaked'] = _worker([ScriptedTurn(content='s')])

    assert runtime.agent_names == ('Worker',)
    assert runtime.agent_factory('Sneaked') is None
    await runtime.shutdown()


async def test_an_unknown_role_name_answers_none_rather_than_raising():
    runtime = Runtime()
    assert runtime.agent_factory('Worker') is None
    await runtime.shutdown()


async def test_the_factory_that_built_an_agent_is_the_one_a_fork_reuses():
    runtime = Runtime()
    factory = _worker([ScriptedTurn(content='x')])
    agent_id = await runtime.spawn(factory, query='go')
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)

    assert runtime.factory_of(agent_id) is factory
    assert runtime.factory_of('nobody') is None
    await runtime.shutdown()


async def test_the_provenance_of_an_agent_this_process_does_not_hold_is_the_worst_case():
    runtime = Runtime()
    assert runtime.provenance_of('nobody') is TrustLevel.UNTRUSTED
    await runtime.shutdown()


# ------------------------------------------------- Delegate 的形状约束


async def test_delegate_creates_its_child_as_a_zombie_so_the_returned_id_is_not_dangling():
    """`ExitMode.EXIT` 会让子 Agent 做完即消失，而返回值里那个 agent_id 就成了悬空引用。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='x')])})
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    slot = runtime._slots[result.meta['agent_id']]
    assert slot.exit_mode is ExitMode.ZOMBIE
    await runtime.shutdown()


async def test_neither_orchestration_tool_declares_itself_safe_to_run_concurrently():
    """两者都写共享状态（注册表、实例表），而并发段还会让 `_refuse_self` 认不出「自己」。"""
    runtime = Runtime()
    assert Delegate(runtime, 'a').is_concurrency_safe({}) is False
    assert Fork(runtime, 'a').is_concurrency_safe({}) is False
    await runtime.shutdown()


async def test_both_tools_describe_when_not_to_use_them_because_that_is_all_the_model_sees():
    """spec §4.6：`description` 必须写「什么时候该用我」，而它是编排者派活的唯一依据。"""
    runtime = Runtime()
    assert 'SendMessage' in Delegate(runtime, 'a').description
    assert 'Fork' in Delegate(runtime, 'a').description
    assert 'Delegate' in Fork(runtime, 'a').description
    assert set(Delegate(runtime, 'a').parameters['required']) == {'agent', 'query'}
    assert set(Fork(runtime, 'a').parameters['required']) == {'query'}
    await runtime.shutdown()
