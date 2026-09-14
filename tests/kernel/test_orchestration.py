"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.agent_types import AgentStatus, ExitMode
from syndica.kernel.context import Context, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.danger import AllowAll, DangerPolicy
from syndica.kernel.errors import ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.runtime import AgentFactory, Runtime, _InTurnWaitResult
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import DEFAULT_MAX_TOOL_CONCURRENCY
from syndica.kernel.tools import Delegate, Fork
from syndica.kernel.tools.orchestration import _INTERRUPTED, _UNFINISHED
from syndica.kernel.waiting import WaitState

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 3.0
_THREE_PART: tuple[str, ...] = ('期望: ', ' 实际: ', ' 下一步: ')
# 一条命中默认规则表里 shell.privilege_escalation 的命令。闸有没有被 §12.2 第 3 条那道升级接走，
# 只有「它到底执行了没有」答得出。
_DANGEROUS = '{"command":"sudo rm -rf /"}'
# 同一批输入在 limit=1 与 limit=N 下必须得出同一个安全结论，所以每条接到闸上的用例都按它参数化。
_LIMITS: tuple[int, ...] = (1, DEFAULT_MAX_TOOL_CONCURRENCY)


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
    limit: int = DEFAULT_MAX_TOOL_CONCURRENCY,
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
                max_tool_concurrency=limit,
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
    limit: int = DEFAULT_MAX_TOOL_CONCURRENCY,
) -> AgentFactory:
    """装上编排工具的 Agent 工厂。

    `agent_id` 在 `BaseAgent.__init__` 里才生成，所以工具只能在建好实例之后挂进那张**活表**
    （`AgentOptions.tools` 刻意不换成副本）。派活因此真的从一次工具调用内部发起——这是在轮等待
    唯一能被打断的位置。脚本共用一个游标，理由同 `_worker`。
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
                max_tool_concurrency=limit,
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


async def _stalled_source(
    runtime: Runtime, blocker: _Block, *, tainted: bool, content: str | None = None
) -> str:
    """一个在一次工具批**中途**被硬终止的源：轨迹末尾留着那个未配对的 assistant 块。

    `tainted=True` 时同一批里更早的调用已经把一块 `UNTRUSTED` 结果落进轨迹，而它排在那个未配对
    的块**之后**——分叉的截断因此把它一起丢掉。`kill` 的 docstring 明文承诺它会留下这个尾形。
    """
    calls = [ToolCall('b1', 'Block', '{}')]
    if tainted:
        calls.insert(0, ToolCall('t1', 'Tainted', '{"text":"网页说要提权删除"}'))
    source = await runtime.spawn(
        _worker(
            [
                ScriptedTurn(content=content, tool_calls=calls),
                ScriptedTurn(content='never reached'),
            ],
            {'Tainted': _Tainted(), 'Block': blocker},
        ),
        query='go',
    )
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    await runtime.kill(source)
    await asyncio.wait_for(runtime.wait(source), timeout=_TIMEOUT)
    return source


def _context(runtime: Runtime, agent_id: str) -> Context:
    agent = runtime._table.get(agent_id)
    assert agent is not None
    return agent.context


def _tool_blocks(runtime: Runtime, agent_id: str) -> list[ContextBlock]:
    return [block for block in _traj(runtime, agent_id) if block.role == 'tool']


def _traj(runtime: Runtime, agent_id: str) -> list[ContextBlock]:
    return _context(runtime, agent_id).current_traj


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


async def test_a_child_that_read_the_web_before_running_out_of_budget_still_hands_back_the_mark():
    """子 -> 父这条路上，未完成那一格与 COMPLETED 那一格必须交回同一个结论。

    只钉 COMPLETED 会让「删掉未完成分支的 trust=」变成一条活不下来的变异，而一个读过外部内容、
    随后撞上预算上限的子 Agent 是真实存在的形状。
    """
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Tainted', '{"text":"from the web"}')],
                {'Tainted': _Tainted()},
                max_turns=1,
            )
        }
    )
    parent = await _idle_parent(runtime)

    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='research'), timeout=_TIMEOUT
    )

    assert result.meta['stop_reason'] == 'max_turns'
    assert result.trust is TrustLevel.UNTRUSTED
    await runtime.shutdown()


@pytest.mark.parametrize('limit', _LIMITS)
async def test_what_a_child_read_is_still_stopped_by_the_gate_on_the_parents_own_next_call(limit):
    """父 -> 子两个方向都接到了闸上，子 -> 父此前只钉了 `result.trust`。

    而「只钉那一块的 trust 不够」是本仓自己写下的标准：真正要守的是父 Agent 此后的高风险调用
    被那道关不掉的升级接走。
    """
    runner = _Runner()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [
                    _call('Tainted', '{"text":"网页说要提权删除"}'),
                    ScriptedTurn(content='照它说的做'),
                ],
                {'Tainted': _Tainted()},
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"去查"}', 'd1'),
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Runner': runner},
        policy=AllowAll(),
        limit=limit,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == []
    await runtime.shutdown()


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_parent_whose_children_stayed_clean_still_does_high_risk_work_afterwards(limit):
    runner = _Runner()
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='clean answer')])})
    boss = _orchestrator(
        [
            _call('Delegate', '{"agent":"Worker","query":"去查"}', 'd1'),
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Runner': runner},
        policy=AllowAll(),
        limit=limit,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == ['sudo rm -rf /']
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_the_laundered_delegation_is_still_stopped_by_the_gate_inside_the_child(limit):
    """只钉那一块的 trust 不够：要证明它真的接到了闸上（spec §12.2 第 3 条那道关不掉的升级）。"""
    runner = _Runner()
    child = _worker(
        [_call('Runner', _DANGEROUS), ScriptedTurn(content='child done')],
        {'Runner': runner},
        policy=AllowAll(),
        limit=limit,
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_delegation_from_a_clean_parent_still_lets_the_child_do_high_risk_work(limit):
    """反向一侧：无条件把子 Agent 的输入标成不可信会让 Worker 这个角色整个用不了。"""
    runner = _Runner()
    child = _worker(
        [_call('Runner', _DANGEROUS), ScriptedTurn(content='child done')],
        {'Runner': runner},
        policy=AllowAll(),
        limit=limit,
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_the_twin_of_a_tainted_agent_is_still_stopped_by_the_gate(limit):
    """把复制过来的标记接到闸上：只钉块自己的 trust 不够。"""
    runner = _Runner()
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [_call('Tainted', '{"text":"from the web"}'), ScriptedTurn(content='conclusion')],
            {'Tainted': _Tainted()},
            policy=AllowAll(),
            limit=limit,
        ),
        query='go',
    )
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = await runtime.spawn(
        _worker(
            [_call('Runner', _DANGEROUS), ScriptedTurn(content='twin done')],
            {'Runner': runner},
            policy=AllowAll(),
            limit=limit,
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_forking_carries_the_taint_that_the_unpaired_truncation_drops(limit):
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
        limit=limit,
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_fork_whose_source_is_clean_still_lets_the_twin_do_high_risk_work(limit):
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
        limit=limit,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == ['sudo rm -rf /']
    await runtime.shutdown()


async def test_a_fork_keeps_the_conclusion_the_model_wrote_on_the_very_block_that_forked():
    """模型最常见的输出形态是在同一块 assistant 上先写下结论、再发起分叉。

    配对不变式要求丢掉的是那些没有结果的 `tool_calls`，**不是 `content`**；连 content 一起截掉
    就删掉了这份轨迹里唯一的那句结论，而 `Fork` 交给模型的 description 逐字写着「分身看得到你到
    此刻为止的轨迹，因此不必重复前提」。
    """
    runtime = Runtime()
    boss = _orchestrator(
        [
            ScriptedTurn(
                content='my hard-won conclusion',
                tool_calls=[ToolCall('f1', 'Fork', '{"query":"keep going"}')],
            ),
            ScriptedTurn(content='fork done'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = _tool_blocks(runtime, parent)[0].extra['tool_meta']['agent_id']
    forked = _traj(runtime, child_id)
    assert [(block.role, block.content) for block in forked[:3]] == [
        ('user', 'go'),
        ('assistant', 'my hard-won conclusion'),
        ('user', 'keep going'),
    ]
    assert [call.id for block in forked for call in block.tool_calls] == []
    await runtime.shutdown()


@pytest.mark.parametrize('content', [None, ''], ids=['no-content', 'empty-string-content'])
async def test_a_forking_block_that_carries_no_text_is_dropped_whole_rather_than_left_empty(
    content,
):
    """既无 content 又无 tool_calls 的块会污染此后每一次请求的前缀，所以剥空之后不能留着它。

    两档取值都要：provider 对「只发了工具调用」这一轮既可能回 `null` 也可能回 `""`，而判据写成
    `content is None` 时后者会留下一个空块。
    """
    runtime = Runtime()
    boss = _orchestrator(
        [
            ScriptedTurn(
                content=content,
                tool_calls=[ToolCall('f1', 'Fork', '{"query":"twin work"}')],
            ),
            ScriptedTurn(content='fork done'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    child_id = _tool_blocks(runtime, parent)[0].extra['tool_meta']['agent_id']
    forked = _traj(runtime, child_id)
    assert [block.role for block in forked] == ['user', 'user', 'assistant']
    assert all(block.content for block in forked)
    await runtime.shutdown()


async def test_stripping_the_unpaired_calls_copies_that_block_instead_of_writing_into_the_source():
    """截断跑在**源 Agent 自己的块**上（深拷在它之后），所以剥 `tool_calls` 必须剥到一份副本上。

    就地剥掉，源的轨迹里那一批声明过的调用就没了：它被复活时 `_repair_interrupted_batch` 再也
    找不到要补的东西，而「这些调用发生过、副作用未知」是那次修复的全部依据。

    交给分身的那一份则**除 `tool_calls` 之外逐字段照抄**：`trust` 漏掉是一次少判，`content`
    漏掉就删掉了模型写下的结论。
    """
    blocker = _Block()
    runtime = Runtime()
    source = await _stalled_source(runtime, blocker, tainted=True, content='源自己写下的那一句')
    head = _traj(runtime, source)[1]
    head.trust = TrustLevel.UNTRUSTED
    head.extra['marked_by'] = 'a hook'

    twin = await runtime.spawn(
        _worker([ScriptedTurn(content='twin')]), query='onward', fork_from=source
    )

    assert [
        (block.role, [call.id for call in block.tool_calls]) for block in _traj(runtime, source)
    ] == [('user', []), ('assistant', ['t1', 'b1']), ('tool', [])]
    inherited = _traj(runtime, twin)[1]
    assert (inherited.role, inherited.content, inherited.trust, inherited.extra) == (
        'assistant',
        '源自己写下的那一句',
        TrustLevel.UNTRUSTED,
        {'marked_by': 'a hook'},
    )
    assert inherited.tool_calls == []
    blocker.release.set()
    await runtime.shutdown()


async def test_a_source_that_stopped_after_a_paired_batch_hands_that_whole_batch_to_the_twin():
    """末尾的批次配对完整时整批照抄：撞 `max_turns` 或轮边界中止都会留下这个尾形。

    无条件截断最后一个带 `tool_calls` 的批次会让整批静默消失，而 spec §5.2 纪律 4 的缓存亲和
    正是分叉的全部意义——分身的第一次请求于是从那一批开始全量 miss。
    """
    runtime = Runtime()
    parent = await runtime.spawn(
        _worker(
            [_call('Tainted', '{"text":"from the web"}')],
            {'Tainted': _Tainted()},
            max_turns=1,
        ),
        query='go',
    )
    ctx = await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    assert ctx.stop_reason is StopReason.MAX_TURNS
    source = _traj(runtime, parent)
    assert [block.role for block in source] == ['user', 'assistant', 'tool']

    child_id = await runtime.spawn(
        _worker([ScriptedTurn(content='twin')]), query='onward', fork_from=parent
    )

    forked = _traj(runtime, child_id)
    assert [
        (block.role, block.content, [call.id for call in block.tool_calls])
        for block in forked[: len(source)]
    ] == [(block.role, block.content, [call.id for call in block.tool_calls]) for block in source]
    await runtime.shutdown()


# ------------------------------------ Fork：query=None（lazy boot）那一格的标记


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_lazy_booted_twin_of_a_tainted_source_is_still_stopped_by_the_gate(limit):
    """`query=None` 是 spec §4.7 明文支持的 lazy boot，而它没有 query 可挂那个补偿。

    截断丢掉了这份轨迹里唯一的不可信标记，而「丢掉了一块」这个事实在 `spawn` 返回的那一刻就不
    存在于任何地方——宿主无从得知，所以下游的 `send(trust=...)` 补不上它。
    """
    blocker = _Block()
    runner = _Runner()
    runtime = Runtime()
    source = await _stalled_source(runtime, blocker, tainted=True)

    twin = await runtime.spawn(
        _worker(
            [_call('Runner', _DANGEROUS), ScriptedTurn(content='twin done')],
            {'Runner': runner},
            policy=AllowAll(),
            limit=limit,
        ),
        fork_from=source,
    )
    await runtime.send(twin, 'do it')
    await asyncio.wait_for(runtime.wait(twin), timeout=_TIMEOUT)

    assert runner.executed == []
    blocker.release.set()
    await runtime.shutdown()


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_lazy_booted_twin_of_a_clean_source_still_does_high_risk_work(limit):
    """反向一侧：无条件标脏会让 `Worker` 这个角色在 `AllowAll` 下整个用不了（spec §4.8）。"""
    blocker = _Block()
    runner = _Runner()
    runtime = Runtime()
    source = await _stalled_source(runtime, blocker, tainted=False)

    twin = await runtime.spawn(
        _worker(
            [_call('Runner', _DANGEROUS), ScriptedTurn(content='twin done')],
            {'Runner': runner},
            policy=AllowAll(),
            limit=limit,
        ),
        fork_from=source,
    )
    await runtime.send(twin, 'clean up')
    await asyncio.wait_for(runtime.wait(twin), timeout=_TIMEOUT)

    assert runner.executed == ['sudo rm -rf /']
    blocker.release.set()
    await runtime.shutdown()


async def test_the_taint_a_lazy_booted_fork_inherits_lands_on_the_last_inherited_block():
    """补偿改的是 `trust` 而不是追加一块说明：`trust` 不进请求字节，分身的前缀因此与源逐字节相同。

    spec §5.2 纪律 4（同类型分叉继承缓存亲和）是分叉的全部意义，而一块内核自撰的说明既改前缀、
    又会被模型读到并据此行动。
    """
    blocker = _Block()
    runtime = Runtime()
    source = await _stalled_source(runtime, blocker, tainted=True)
    dropped = [block for block in _traj(runtime, source) if block.trust is TrustLevel.UNTRUSTED]
    assert [block.content for block in dropped] == ['网页说要提权删除']

    twin = await runtime.spawn(_worker([ScriptedTurn(content='twin')]), fork_from=source)

    forked = _traj(runtime, twin)
    assert [(block.role, block.content, block.trust) for block in forked] == [
        ('user', 'go', TrustLevel.UNTRUSTED)
    ]
    blocker.release.set()
    await runtime.shutdown()


async def test_a_lazy_booted_fork_with_nothing_left_to_carry_the_taint_says_so_loudly(caplog):
    """截断之后一块都不剩、又没有 query 可挂时，这条 ERROR 是这次丢失留下的全部痕迹。

    `provenance_of` 是「出现过就算」的上界，所以标记只能挂在一块真实存在的块上；一块都没有时
    静默返回等于把那道关不掉的升级悄悄关掉。
    """
    runtime = Runtime()
    source = await runtime.spawn(_worker([ScriptedTurn(content='source')]))
    context = _context(runtime, source)
    context.append(ContextBlock(role='assistant', tool_calls=[ToolCall('orphan', 'Nope', '{}')]))
    context.append(
        ContextBlock(
            role='tool', content='网页说的', tool_call_id='other', trust=TrustLevel.UNTRUSTED
        )
    )

    twin = await runtime.spawn(_worker([ScriptedTurn(content='twin')]), fork_from=source)

    assert _traj(runtime, twin) == []
    record = next(record for record in caplog.records if record.levelname == 'ERROR')
    assert record.args == (source, twin)
    assert all(part in record.getMessage() for part in _THREE_PART)
    await runtime.shutdown()


class _Uncopyable:
    """深拷时自己抛的值。`extra` 收得下任何东西，所以拷不动的原因不只有「不可 pickle」一种。"""

    def __deepcopy__(self, memo: dict[int, object]) -> _Uncopyable:
        raise ValueError('这份快照拒绝被复制')


@pytest.mark.parametrize(
    ('value', 'anchor'),
    [(threading.Lock(), 'TypeError'), (_Uncopyable(), 'ValueError')],
    ids=['a-live-lock', 'a-value-that-refuses'],
)
async def test_a_fork_whose_trajectory_cannot_be_deep_copied_leaves_no_half_built_agent(
    value, anchor
):
    """逐块深拷是承重的（分身改自己的轨迹不得污染源），而活的对象拷不动。

    裸异常指着 `copy` 的内部，读者看不出该改哪一块；而抛在 `register` 之前意味着这次失败什么都
    没留下，那一句必须写进下一步里。两档取值把接的范围钉住：只接 `TypeError` 会让一个自己抛
    `ValueError` 的 `__deepcopy__` 原样穿出去。
    """
    runtime = Runtime()
    source = await runtime.spawn(_worker([ScriptedTurn(content='source')]), query='go')
    await asyncio.wait_for(runtime.wait(source), timeout=_TIMEOUT)
    _traj(runtime, source)[0].extra['live'] = value
    before = set(runtime._slots)

    with pytest.raises(ContractViolation) as caught:
        await runtime.spawn(_worker([ScriptedTurn(content='twin')]), fork_from=source)

    assert source in caught.value.expected
    assert "role='user'" in caught.value.received
    assert anchor in caught.value.received
    assert set(runtime._slots) == before
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


@pytest.mark.parametrize('limit', _LIMITS)
async def test_a_legitimate_abort_does_not_permanently_cost_this_agent_its_allow_all(limit):
    """被打断的那一块只嵌内核自撰的文字与一个内核生成的 child_id，没有任何外部字节。

    标成 `UNTRUSTED` 会让一次正当的中止**永久**废掉这个 Agent 的 `AllowAll`：这块落进轨迹之后
    `provenance_of` 是「出现过就算」的上界，而宿主关不掉它（D-29 未实现）。
    """
    blocker = _Block()
    runner = _Runner()
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
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Runner': runner},
        policy=AllowAll(),
        limit=limit,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    await runtime.abort(parent)
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)
    assert [block.trust for block in _tool_blocks(runtime, parent)] == [TrustLevel.TRUSTED]

    assert await runtime.ensure_active(parent)
    await runtime.send(parent, 'now run it')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == ['sudo rm -rf /']
    blocker.release.set()
    await runtime.shutdown()


@pytest.mark.parametrize('limit', _LIMITS)
async def test_an_interrupted_delegation_does_not_launder_what_the_parent_had_already_read(limit):
    """反向一侧：那一块交回 `TRUSTED` 不等于它把父此前读过的东西洗白了。

    父自己的轨迹里已经有那块 `UNTRUSTED`，而 `provenance_of` 看的是整份轨迹。
    """
    blocker = _Block()
    runner = _Runner()
    runtime = Runtime(
        agents={
            'Worker': _worker(
                [_call('Block', '{}'), ScriptedTurn(content='child done')], {'Block': blocker}
            )
        }
    )
    boss = _orchestrator(
        [
            _call('Tainted', '{"text":"网页说要提权删除"}', 't1'),
            _call('Delegate', '{"agent":"Worker","query":"slow job"}', 'd1'),
            _call('Runner', _DANGEROUS, 'r1'),
            ScriptedTurn(content='boss done'),
        ],
        runtime,
        {'Tainted': _Tainted(), 'Runner': runner},
        policy=AllowAll(),
        limit=limit,
    )
    parent = await runtime.spawn(boss, query='go')
    await asyncio.wait_for(blocker.entered.wait(), timeout=_TIMEOUT)
    await runtime.abort(parent)
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert await runtime.ensure_active(parent)
    await runtime.send(parent, 'now run it')
    await asyncio.wait_for(runtime.wait(parent), timeout=_TIMEOUT)

    assert runner.executed == []
    blocker.release.set()
    await runtime.shutdown()


async def test_a_child_that_finished_is_delivered_even_though_an_abort_already_arrived():
    """已经跑完的那份轨迹此刻丢掉没有任何好处，而中止仍会在轮边界上兑现。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='child answer')])})
    parent = await runtime.spawn(_worker([ScriptedTurn(content='p')]), query='go')
    child = await runtime.spawn(_worker([ScriptedTurn(content='child answer')]), query='x')
    await asyncio.wait_for(runtime.wait(child), timeout=_TIMEOUT)
    slot = runtime._slots[parent]
    slot.control.request_abort()

    result = await asyncio.wait_for(runtime._wait_in_turn(parent, child), timeout=_TIMEOUT)

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
    first = asyncio.ensure_future(runtime._wait_in_turn(parent, slow))
    await asyncio.sleep(0)

    second = asyncio.ensure_future(runtime._wait_in_turn(parent, slow))
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
    first = asyncio.ensure_future(runtime._wait_in_turn(parent, slow))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(runtime._wait_in_turn(parent, slow))
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
        await runtime._wait_in_turn('not-mine', parent)
    assert 'not-mine' in caught.value.expected
    await runtime.shutdown()


async def test_waiting_in_turn_on_oneself_is_refused_instead_of_parking_forever():
    """等自己的事件循环结束就是永久挂死：它停不下来，正是因为这一轮悬在这次等待上。

    `despawn` / `kill` 为完全同型的情形立了守卫；判据取**两个 id 相等**而不是任务身份，因此它在
    并发段里（`_refuse_self` 认不出自己的那一格，D-39）同样成立。
    """
    runtime = Runtime()
    parent = await _idle_parent(runtime)

    with pytest.raises(ContractViolation) as caught:
        await asyncio.wait_for(runtime._wait_in_turn(parent, parent), timeout=_TIMEOUT)

    assert parent in caught.value.received
    assert 'abort' in caught.value.next_action
    assert runtime._slots[parent].delegation is None
    await runtime.shutdown()


async def test_waiting_in_turn_for_a_target_this_process_never_saw_says_so_in_its_own_words():
    """借用 `wait()` 的那条消息会把下一步讲成「用 wait() 读轨迹」——写给 Runtime 外面的调用方。

    在轮等待的调用方是发起方自己的编排工具，它这一轮悬在这次等待上。
    """
    runtime = Runtime()
    parent = await _idle_parent(runtime)

    with pytest.raises(ContractViolation) as caught:
        await asyncio.wait_for(runtime._wait_in_turn(parent, 'nobody'), timeout=_TIMEOUT)

    assert 'nobody' in caught.value.expected
    assert parent in caught.value.next_action
    assert 'ZOMBIE' in caught.value.next_action
    assert runtime._slots[parent].delegation is None
    await runtime.shutdown()


async def test_a_delegation_that_returned_normally_leaves_the_delegation_slot_empty():
    """槽不释放，此后每一次 `abort` 都会打在一个早已返回的等待上，而真正在飞的那次打不到。"""
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='child answer')])})
    parent = await _idle_parent(runtime)

    await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert runtime._slots[parent].delegation is None
    await runtime.shutdown()


@pytest.mark.parametrize(
    ('state', 'context'),
    [
        (WaitState.ANSWERED, None),
        (WaitState.ABORTED, Context()),
        (WaitState.PREEMPTED, Context()),
        (WaitState.TIMEOUT, Context()),
    ],
    ids=['answered-without-one', 'aborted-with-one', 'preempted-with-one', 'timeout-with-one'],
)
def test_an_in_turn_wait_result_whose_trajectory_disagrees_with_its_state_is_refused(
    state, context
):
    """没有轨迹的 `answered` 在调用方那里是一次拿不到结果的成功，反过来是把「它还在跑」读成
    「它已经跑完」。两者都是产出方的缺陷，在构造处拦下比在使用处现身便宜得多。
    """
    with pytest.raises(ContractViolation) as caught:
        _InTurnWaitResult(state=state, context=context)

    assert state.name in caught.value.received
    assert 'answered' in caught.value.next_action


@pytest.mark.parametrize(
    ('state', 'context'),
    [(WaitState.ANSWERED, Context()), (WaitState.ABORTED, None)],
    ids=['answered-with-one', 'aborted-without-one'],
)
def test_the_two_consistent_shapes_of_an_in_turn_wait_result_are_accepted(state, context):
    assert _InTurnWaitResult(state=state, context=context).state is state


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

    assert runtime._factory_of(agent_id) is factory
    assert runtime._factory_of('nobody') is None
    await runtime.shutdown()


async def test_the_provenance_of_an_agent_this_process_does_not_hold_is_the_worst_case():
    runtime = Runtime()
    assert runtime._provenance_of('nobody') is TrustLevel.UNTRUSTED
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


async def test_the_conclusion_is_found_even_when_a_hook_appended_after_the_final_assistant_block():
    """`AGENT_END` 收到的 `ctx` 就是子 Agent 那一份，切点之后它的末尾可以是任何东西。

    取最后一块就会把一个观察者写下的块当成子 Agent 的结论交给模型。
    """
    runtime = Runtime(agents={'Worker': _worker([ScriptedTurn(content='the child answer')])})
    parent = await _idle_parent(runtime)

    async def append_a_trailing_block(**kwargs: object) -> HookResult:
        ctx = kwargs['ctx']
        assert isinstance(ctx, Context)
        if [block.content for block in ctx.current_traj if block.role == 'assistant'] == [
            'the child answer'
        ]:
            ctx.append(
                ContextBlock(role='tool', content='an observer wrote this', tool_call_id='z')
            )
        return HookResult()

    runtime.hooks.register(Junction.AGENT_END, append_a_trailing_block)
    result = await asyncio.wait_for(
        Delegate(runtime, parent).execute(agent='Worker', query='x'), timeout=_TIMEOUT
    )

    assert 'the child answer' in str(result.output)
    assert 'an observer wrote this' not in str(result.output)
    await runtime.shutdown()


def test_the_unfinished_table_answers_for_every_stop_reason_and_for_having_none():
    """两张表都按枚举取全：缺一个键就是一个 `KeyError` 从工具里上抛、把整次 run 打成 error。

    期望值来自枚举本身（与这两张表无关的第三方事实源），所以新增一个 `StopReason` 成员就变红。
    """
    assert set(_UNFINISHED) == {None, *StopReason}
    assert [key for key, text in _UNFINISHED.items() if not isinstance(text, str) or not text] == []


def test_the_interrupted_table_answers_for_every_wait_state():
    assert set(_INTERRUPTED) == set(WaitState)
    assert [
        key for key, text in _INTERRUPTED.items() if not isinstance(text, str) or not text
    ] == []


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
