"""端到端：spec §15 里程碑 1 的完成判据。

「Runtime 能创建多个 Agent、互发消息、各自跑通『LLM + 工具』循环、可终止与恢复」——四条判据各有
能单独指向它的断言，逐条标在下面的注释里。

**全程只从 `syndica.kernel` 导入**，因此这条用例同时是公开面完整性的见证者：一个只看得见包面的
宿主导不到的东西，在这里就是一条 ImportError。

只有 LLM 是假的。Runtime、事件循环、broker、控制块、门、hook、工具、危险闸与等待都是真的
（spec §13.1）。

**脚本化的假模型读不了 `Delegate` 交回的那行 id**，而真模型读得了——所以这里让工厂在造出实例的那
一刻记下 `agent_id`：工厂在 `spawn` 的第一步被调用，一个角色的 id 因此在此后每个角色的工厂里都读
得到。用例据此写出「B 给 A 发消息」，而不是把 Runtime 换成别的东西。

本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时是
字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from syndica.kernel import (
    AgentFactory,
    AgentOptions,
    AgentStatus,
    BaseAgent,
    Event,
    EventBus,
    ExitMode,
    HooksCenter,
    Reply,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    Trigger,
    WaitState,
)

# 只加在真的可能挂住的调用上（等别的执行体的任务）。控制面的问句不加——多一层 Task 会插进一次
# 调度跳转，把本用例要观测的那两个「落在一轮中间」的窗口改掉。
_TIMEOUT = 5.0
# 一次「本用例期间绝不会到期」的等待上限：它要挡的回归是「明明没等到也没被打断，却提前返回了」，
# 而那种返回会被误读成超时。
_UNREACHED_WAIT = 30.0


async def _status_reaching(
    runtime: Runtime, agent_id: str, status: AgentStatus
) -> AgentStatus | None:
    """轮询注册表直到读到 `status` 或 `_TIMEOUT` 用完，交回最后读到的状态。

    工具置位「已进入等待」与注册表写下 WAITING 之间隔着几次 await，只读一次时余量恰好一跳，内核多
    一个 await 就误报。WAITING 要等到答复或中止才会离开，所以轮询不会越过要观测的那个窗口。上界用
    `asyncio.timeout` 而不是 `wait_for`：后者多包一层 Task，会插进一次调度跳转。
    """
    seen: AgentStatus | None = None
    try:
        async with asyncio.timeout(_TIMEOUT):
            while seen is not status:
                info = await runtime.find(agent_id)
                seen = None if info is None else info.status
                if seen is not status:
                    await asyncio.sleep(0.005)
    except TimeoutError:
        pass
    return seen


# 一个 Agent 自己的工具排在编排五件套之前，而工具表的迭代顺序逐字进请求前缀（spec §5.2）。
# 七个元素，既非升序（Delegate / Fork / KillAgent / Ledger / Probe / QueryAgent / SendMessage）
# 亦非降序，反转后同样两者都不是。
_PREFIX_TOOLS = [
    'Ledger',
    'Probe',
    'Delegate',
    'Fork',
    'SendMessage',
    'QueryAgent',
    'KillAgent',
]


class Ledger(Tool):
    """三个 Agent 共用的一本流水账：条目的先后就是工具执行的全局先后。"""

    __tool_command_args__ = ()

    def __init__(self) -> None:
        self.entries: list[str] = []

    async def execute(self, entry: str) -> ToolResult:
        """把 entry 记进流水账。"""
        self.entries.append(entry)
        return ToolResult(output=f'recorded: {entry}', meta={'position': len(self.entries)})


class Probe(Tool):
    """声明可并发：它不写任何共享状态，所以同一批里的几次调用归进同一段（spec §5.6）。"""

    __tool_command_args__ = ()

    def is_concurrency_safe(self, args: dict[str, object]) -> bool:
        return True

    async def execute(self, site: str) -> ToolResult:
        """读一个点位。"""
        return ToolResult(output=f'site {site} is clear')


class AwaitReply(Tool):
    """在一轮的中间等一条按关联 ID 匹配的答复（spec §4.2 的 `wait_for`）。

    `entered` 里每个 tag 各一个事件，用例按 tag 等：一个反复置位再清位的事件分不出「第一次调用
    还没退出」与「第二次调用已经进来了」，而这两次调用的结局刻意不同。
    """

    __tool_command_args__ = ()

    def __init__(
        self, host: dict[str, Runtime], cast: dict[str, str], entered: dict[str, asyncio.Event]
    ) -> None:
        self._host = host
        self._cast = cast
        self._entered = entered
        self.states: list[WaitState] = []

    async def execute(self, tag: str) -> ToolResult:
        """等一条 correlation_id 等于 tag 的答复。"""
        self._entered[tag].set()
        result = await self._host['runtime'].wait_for(
            self._cast['Surveyor'], Reply(correlation_id=tag, timeout=_UNREACHED_WAIT)
        )
        self.states.append(result.state)
        answer = None if result.message is None else result.message.content
        return ToolResult(output=f'{result.state.value}:{answer}')


def _call(call_id: str, name: str, **arguments: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))


def _role(
    name: str,
    script: Callable[[dict[str, str]], list[ScriptedTurn]],
    tools: dict[str, Tool],
    cast: dict[str, str],
    llms: dict[str, ScriptedLLM],
) -> AgentFactory:
    """一个角色的工厂：造实例、记下它的 id、把它的假模型留给用例读。

    `dict(tools)` 而不是原表：一张被多个 Agent 共用的工具表会在第一次装配时变成「属于第一个
    Agent」。共用的是工具**实例**（流水账要跨 Agent 看先后），不是那张表。
    """

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        llm = ScriptedLLM(script(cast))
        agent = BaseAgent(
            AgentOptions(
                name=name,
                llm=llm,
                tools=dict(tools),
                system_prompt=f'You are the {name}.',
            ),
            hooks=hooks,
            events=events,
        )
        cast[name] = agent.agent_id
        llms[name] = llm
        return agent

    return build


async def test_a_runtime_drives_three_agents_that_message_each_other_and_one_resumes_an_abort():
    ledger = Ledger()
    probe = Probe()
    cast: dict[str, str] = {}
    llms: dict[str, ScriptedLLM] = {}
    host: dict[str, Runtime] = {}
    entered = {'north-sign-off': asyncio.Event(), 'second-sign-off': asyncio.Event()}
    await_reply = AwaitReply(host, cast, entered)

    def coordinator_script(_: dict[str, str]) -> list[ScriptedTurn]:
        return [
            # 一批两个可并发的调用：它们归进同一段，而「一次调用恰好一块结果、按调用顺序落」
            # 这条不变式不因为并发而改变。
            ScriptedTurn(
                tool_calls=[
                    _call('p1', 'Probe', site='north'),
                    _call('p2', 'Probe', site='south'),
                ]
            ),
            ScriptedTurn(tool_calls=[_call('o1', 'Ledger', entry='coordinator:plan')]),
            ScriptedTurn(
                tool_calls=[
                    _call('o2', 'Delegate', agent='Surveyor', query='survey the north field')
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    _call('o3', 'Delegate', agent='Courier', query='ask the surveyor to re-check')
                ]
            ),
            ScriptedTurn(content='the north field is mapped'),
        ]

    def surveyor_script(known: dict[str, str]) -> list[ScriptedTurn]:
        coordinator = known['Coordinator']
        return [
            # 第一个周期：`Delegate` 冷启动它。
            ScriptedTurn(tool_calls=[_call('s1', 'Ledger', entry='surveyor:north')]),
            ScriptedTurn(
                tool_calls=[
                    _call(
                        's2',
                        'SendMessage',
                        agent_id=coordinator,
                        content='north field has 3 sites',
                    )
                ]
            ),
            ScriptedTurn(content='survey complete'),
            # 第二个周期：Courier 的一条消息把它从 ZOMBIE 叫醒。两次轮中等待，一次等到答复、
            # 一次被中止打断。
            ScriptedTurn(tool_calls=[_call('s4', 'Ledger', entry='surveyor:recheck')]),
            ScriptedTurn(tool_calls=[_call('s5', 'AwaitReply', tag='north-sign-off')]),
            ScriptedTurn(tool_calls=[_call('s6', 'AwaitReply', tag='second-sign-off')]),
            # 第三个周期：被中止之后，宿主的一条消息把它叫醒，它接着原有轨迹往下写。
            ScriptedTurn(content='north field verified: 3 sites'),
        ]

    def courier_script(known: dict[str, str]) -> list[ScriptedTurn]:
        surveyor = known['Surveyor']
        return [
            ScriptedTurn(tool_calls=[_call('c1', 'Ledger', entry='courier:relay')]),
            ScriptedTurn(
                tool_calls=[
                    _call(
                        'c2',
                        'SendMessage',
                        agent_id=surveyor,
                        content='re-check the north field',
                    )
                ]
            ),
            ScriptedTurn(content='relayed'),
        ]

    runtime = Runtime(
        orchestration=True,
        agents={
            'Surveyor': _role(
                'Surveyor',
                surveyor_script,
                {'Ledger': ledger, 'AwaitReply': await_reply},
                cast,
                llms,
            ),
            'Courier': _role('Courier', courier_script, {'Ledger': ledger}, cast, llms),
        },
    )
    host['runtime'] = runtime
    subscription = runtime.events.subscribe()

    coordinator = await runtime.spawn(
        _role(
            'Coordinator',
            coordinator_script,
            {'Ledger': ledger, 'Probe': probe},
            cast,
            llms,
        ),
        query='map the north field',
        exit_mode=ExitMode.ZOMBIE,
    )
    coordinator_ctx = await asyncio.wait_for(runtime.wait(coordinator), _TIMEOUT)
    surveyor = cast['Surveyor']
    courier = cast['Courier']

    # 判据一：创建多个 Agent。三个互不相同的 id 同时在注册表里，父子关系与 spawn 时声明的一致。
    assert cast['Coordinator'] == coordinator
    assert len({coordinator, surveyor, courier}) == 3
    for agent_id, name, parent in (
        (coordinator, 'Coordinator', None),
        (surveyor, 'Surveyor', coordinator),
        (courier, 'Courier', coordinator),
    ):
        info = await runtime.find(agent_id)
        assert info is not None
        assert (info.agent_id, info.name, info.parent_id) == (agent_id, name, parent)

    # 判据三：各自跑通「LLM + 工具」循环。流水账是跨 Agent 的单一时间轴，三个主体各自调过工具，
    # 且 Surveyor 在被叫醒之后又调了一次。
    assert ledger.entries == [
        'coordinator:plan',
        'surveyor:north',
        'courier:relay',
        'surveyor:recheck',
    ]
    # 工具定义连同次序一起进请求前缀：装上的编排五件套排在这个 Agent 自己那一个之后。
    assert [
        schema['name'] for schema in llms['Coordinator'].call_kwargs[0]['tools']
    ] == _PREFIX_TOOLS

    # 判据二（上行）：Surveyor 写给 Coordinator 的那条消息，进了 Coordinator 的上下文。
    inbound = [
        block
        for block in coordinator_ctx.current_traj
        if block.role == 'user' and block.extra.get('sender') == surveyor
    ]
    assert [block.content for block in inbound] == ['north field has 3 sites']
    assert inbound[0].extra['trigger'] == Trigger.PENDING.value

    # Coordinator 真的派出去并等回了那两个 Agent：`Delegate` 交回的正是它们的 id。同一条断言
    # 顺带钉住并发那一段：一批两个可并发的调用落下两块结果，按调用顺序、各占一块。
    tool_blocks = [block for block in coordinator_ctx.current_traj if block.role == 'tool']
    assert [
        (block.tool_call_id, block.extra.get('tool_meta', {}).get('agent_id'))
        for block in tool_blocks
    ] == [
        ('p1', None),
        ('p2', None),
        ('o1', None),
        ('o2', surveyor),
        ('o3', courier),
    ]
    assert [block.content for block in tool_blocks[:2]] == [
        'site north is clear',
        'site south is clear',
    ]
    assert coordinator_ctx.stop_reason is StopReason.COMPLETED

    # 轮中等待：一条按关联 ID 匹配的答复解开它。
    await asyncio.wait_for(entered['north-sign-off'].wait(), _TIMEOUT)
    assert await _status_reaching(runtime, surveyor, AgentStatus.WAITING) is AgentStatus.WAITING
    answered = await runtime.send(surveyor, 'signed off', correlation_id='north-sign-off')
    assert answered.ok is True

    # 判据四（终止）：中止穿透一次在飞的等待——此刻 Surveyor 正卡在一轮的中间。
    await asyncio.wait_for(entered['second-sign-off'].wait(), _TIMEOUT)
    assert await _status_reaching(runtime, surveyor, AgentStatus.WAITING) is AgentStatus.WAITING

    await runtime.abort(surveyor)
    aborting = await runtime.find(surveyor)
    assert aborting is not None
    assert aborting.status is AgentStatus.ABORTING

    surveyor_ctx = await asyncio.wait_for(runtime.wait(surveyor), _TIMEOUT)
    assert surveyor_ctx.stop_reason is StopReason.ABORTED
    stopped = await runtime.find(surveyor)
    assert stopped is not None
    assert stopped.status is AgentStatus.ZOMBIE
    # 两次等待的结局各不相同：一次被答复解开，一次被中止打断。
    assert await_reply.states == [WaitState.ANSWERED, WaitState.ABORTED]
    # 中止落在轮与轮之间，因此轨迹是配对完整的：每个声明过的调用都有一块结果。
    declared = [call.id for block in surveyor_ctx.current_traj for call in block.tool_calls]
    answers = [block.tool_call_id for block in surveyor_ctx.current_traj if block.role == 'tool']
    assert declared == answers == ['s1', 's2', 's4', 's5', 's6']
    before_resume = [(block.role, block.content) for block in surveyor_ctx.current_traj]

    # 判据四（恢复）：一条消息把它从 ZOMBIE 叫回来，它带着**原有那一份**上下文接着往下写，
    # 而不是又 spawn 了一个。
    delivered = await runtime.send(surveyor, 'report what you have')
    assert delivered.ok is True
    resumed_ctx = await asyncio.wait_for(runtime.wait(surveyor), _TIMEOUT)
    assert resumed_ctx is surveyor_ctx
    assert resumed_ctx.stop_reason is StopReason.COMPLETED
    after_resume = [(block.role, block.content) for block in resumed_ctx.current_traj]
    assert after_resume[: len(before_resume)] == before_resume
    assert len(after_resume) > len(before_resume)
    # 轨迹的头还是第一个周期那句派活，而不是一份从头开始的新上下文。
    assert after_resume[0] == ('user', 'survey the north field')
    assert after_resume[-1] == ('assistant', 'north field verified: 3 sites')
    # 每个 Agent 各调各的模型：Surveyor 跨三个周期共七轮，被中止的那一轮不消耗预算、不调模型。
    assert [len(llms[name].calls) for name in ('Coordinator', 'Surveyor', 'Courier')] == [5, 7, 3]

    # 判据二（下行）：Courier 与宿主写给 Surveyor 的消息各自进了它的上下文，并且带着发送方；
    # 被等到的那条答复**不**在其中——它由 `take_by_correlation` 从邮箱取走，不再注入一遍。
    senders = [
        (block.extra.get('sender'), block.content)
        for block in resumed_ctx.current_traj
        if block.role == 'user'
    ]
    assert senders == [
        (None, 'survey the north field'),
        (courier, 're-check the north field'),
        ('runtime', 'report what you have'),
    ]

    # 判据四（销毁）：整棵树连条目一起消失。
    await asyncio.wait_for(runtime.despawn(coordinator), _TIMEOUT)
    assert await runtime.find(coordinator) is None
    assert await runtime.find(surveyor) is None
    assert await runtime.find(courier) is None

    await asyncio.wait_for(runtime.shutdown(), _TIMEOUT)

    events: list[Event] = [event async for event in subscription]
    assert subscription.dropped == 0
    # 五次工作、三个主体、一次中止与一次接着干：这一条同时钉住谁在跑、跑了几次、各自怎么收场。
    assert [
        (event.agent_id, event.payload['stop_reason'])
        for event in events
        if event.kind == 'agent_end'
    ] == [
        (surveyor, 'completed'),
        (courier, 'completed'),
        (coordinator, 'completed'),
        (surveyor, 'aborted'),
        (surveyor, 'completed'),
    ]
