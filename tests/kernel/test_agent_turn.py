"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸——而工具 schema 会进请求前缀，正是本文件要钉住的东西。
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import Context, ContextBlock, StopReason, ToolCall, TrustLevel
from syndica.kernel.control import BrokerTurnGate, ControlBlock, TurnGate, TurnGateDecision
from syndica.kernel.danger import AllowAll, DangerJudge
from syndica.kernel.errors import (
    ConfigError,
    ContractViolation,
    LLMResponseError,
    ToolFatalError,
)
from syndica.kernel.events import Event, EventBus, Subscription
from syndica.kernel.hooks import HookCallback, HookResult, HooksCenter, Junction
from syndica.kernel.llm import ContextOverflowError, ScriptedLLM, ScriptedTurn
from syndica.kernel.message import Message, Trigger
from syndica.kernel.stores import InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tool_executor import DEFAULT_MAX_TOOL_CONCURRENCY

# 每个等待都设上限：回归应当让用例变红，而不是把 CI 挂住。
_TIMEOUT = 1.0
_SYSTEM_PROMPT = 'You are a worker.'
# 门按 agent_id 认邮箱，而 BaseAgent 的 agent_id 是构造时才生成的 uuid；这些用例只需要门与
# broker 说的是同一个邮箱，用哪个名字无关紧要。
_MAILBOX = 'a1'
# 逐字写死而不是读 tool.schema：期望值与被测对象同源时，schema 缺一半会让期望值跟着缩水。这份
# schema 逐字进模型请求的字节前缀（spec §5.2），键序一并钉住。
_ECHO_SCHEMA: dict[str, Any] = {
    'name': 'Echo',
    'description': '回显 text。',
    'parameters': {
        'type': 'object',
        'properties': {'text': {'type': 'string'}},
        'required': ['text'],
    },
}
# 两个参数而不是一个：单属性下 properties 的键序不可观测，而它同样进请求前缀。
_CONCAT_SCHEMA: dict[str, Any] = {
    'name': 'Concat',
    'description': '拼接 left 与 right。',
    'parameters': {
        'type': 'object',
        'properties': {'left': {'type': 'string'}, 'right': {'type': 'string'}},
        'required': ['left', 'right'],
    },
}
_UPPER_SCHEMA: dict[str, Any] = {
    'name': 'Upper',
    'description': '把 text 转成大写。',
    'parameters': {
        'type': 'object',
        'properties': {'text': {'type': 'string'}},
        'required': ['text'],
    },
}
_TOOL_SCHEMAS: list[dict[str, Any]] = [_ECHO_SCHEMA, _CONCAT_SCHEMA, _UPPER_SCHEMA]
_DANGEROUS = '{"command":"sudo rm -rf /"}'
_LIFECYCLE: tuple[Junction, ...] = (
    Junction.AGENT_START,
    Junction.TURN_START,
    Junction.TURN_END,
    Junction.AGENT_END,
    Junction.AGENT_ERROR,
)


class _Echo(Tool):
    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Concat(Tool):
    async def execute(self, left: str, right: str) -> ToolResult:
        """拼接 left 与 right。"""
        return ToolResult(output=left + right)


class _Upper(Tool):
    async def execute(self, text: str) -> ToolResult:
        """把 text 转成大写。"""
        return ToolResult(output=text.upper())


class _Runner(Tool):
    """把 command 交给 shell 执行。"""

    __tool_command_args__ = ('command',)

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, command: str) -> ToolResult:
        """执行 command。"""
        self.executed.append(command)
        return ToolResult(output=f'ran {command}')


class _Probe(Tool):
    """并发探针：进来记一笔在飞集合，让出一次，再记下让出那一刻同时在跑的是谁。

    让出恰好一次，且不能不让：协程不让出就一路跑到底，同段的同伴根本没机会进来，于是真并发与真
    串行留下同样的快照。
    """

    __tool_command_args__ = ()

    def __init__(self) -> None:
        self._inflight: set[str] = set()
        self._together: list[int] = []

    @property
    def peak(self) -> int:
        return max(self._together, default=0)

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return True

    async def execute(self, label: str) -> ToolResult:
        """回显 label。"""
        self._inflight.add(label)
        await asyncio.sleep(0)
        self._together.append(len(self._inflight))
        self._inflight.discard(label)
        return ToolResult(output=label)


class _Fatal(Tool):
    async def execute(self) -> ToolResult:
        """总是抛致命异常。"""
        raise ToolFatalError(
            expected='目标可写',
            received='路径不存在且父目录不可写',
            next_action='换一个可写路径',
        )


class _Blocking(Tool):
    """停在 release 上，直到测试放开它或它被取消。

    取消与并发这两条路都必须让 run 真的挂在半途：脚本化 LLM 与无 hook 的切点全程不让出控制权，
    靠调度顺序碰不出「上一次还没跑完」这个状态。
    """

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self._entered = entered
        self._release = release

    async def execute(self) -> ToolResult:
        """一直等着。"""
        self._entered.set()
        await self._release.wait()
        return ToolResult(output='released')


class _FixedGate:
    """按固定结论作答的门，并记下自己被问过哪些轮。

    `BrokerTurnGate` 中止时不消费邮箱，因此它永远给不出「既中止又带消息」的结论；而 `TurnGate`
    是公开 Protocol，任何第三方实现都给得出，两个字段的优先级必须有唯一答案。
    """

    def __init__(self, decision: TurnGateDecision) -> None:
        self.decision = decision
        self.turns: list[int] = []

    async def before_turn(self, turn: int) -> TurnGateDecision:
        self.turns.append(turn)
        return self.decision


class _BrokenGate:
    """问一句就抛的门，代表控制面自己出了故障。"""

    async def before_turn(self, turn: int) -> TurnGateDecision:
        raise RuntimeError('control plane unreachable')


class _Publisher(Tool):
    """执行时往自己的邮箱里投一条消息，用来构造「用户在一轮的中途插话」。

    插话必须来自一轮的**中间**：周期开始前就躺在邮箱里的消息，无论门是每周期问一次还是每轮问一
    次都会被注入，区分不出两者。
    """

    def __init__(self, broker: InMemoryMessageBroker, agent_id: str, content: str) -> None:
        self._broker = broker
        self._agent_id = agent_id
        self._content = content

    async def execute(self) -> ToolResult:
        """投一条消息给自己。"""
        await self._broker.publish(
            self._agent_id,
            Message(sender='user', receiver=self._agent_id, content=self._content),
        )
        return ToolResult(output='published')


class _NumberedPrompt(BaseAgent):
    """每次 get_system_prompt 返回不同的值。

    刻意违反「逐轮字节一致」：SP 是否每轮重建，只有在返回值会变时才可观测。
    """

    def __init__(self, options: AgentOptions, *, hooks: HooksCenter, events: EventBus) -> None:
        super().__init__(options, hooks=hooks, events=events)
        self.prompt_calls = 0

    def get_system_prompt(self) -> str:
        self.prompt_calls += 1
        return f'prompt {self.prompt_calls}'


class _RecordingBus(EventBus):
    """把 emit 记进与 hook 共用的一条序列。

    emit 与 agent 的 await 同步，因此这里记下的次序就是跨通道的真实次序；订阅者是另一个 task，
    它看到的交错取决于调度。
    """

    def __init__(self, log: list[tuple[str, str]]) -> None:
        super().__init__()
        self._log = log

    async def emit(self, event: Event) -> None:
        self._log.append(('event', event.kind))
        await super().emit(event)


def _tools() -> dict[str, Tool]:
    """默认工具表。

    三个工具、注册顺序既非升序也非降序：n=1 时反转 / 排序 / 去重 / 取首 / 取尾全是同一个答案，
    而工具定义的次序进请求前缀（spec §5.2）。这个顺序同时区别于 ['Concat','Echo','Upper']、
    ['Upper','Echo','Concat'] 与 ['Upper','Concat','Echo']。
    """
    return {'Echo': _Echo(), 'Concat': _Concat(), 'Upper': _Upper()}


def _agent(
    script: list[ScriptedTurn],
    *,
    tools: dict[str, Tool] | None = None,
    system_prompt: str = _SYSTEM_PROMPT,
    max_turns: int | None = 50,
    events: EventBus | None = None,
    gate: TurnGate | None = None,
) -> tuple[BaseAgent, HooksCenter]:
    hooks = HooksCenter()
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(script),
        tools=_tools() if tools is None else tools,
        system_prompt=system_prompt,
        max_turns=max_turns,
    )
    bus = events if events is not None else EventBus()
    return BaseAgent(options, hooks=hooks, events=bus, gate=gate), hooks


async def _gate(control: ControlBlock) -> tuple[BrokerTurnGate, InMemoryMessageBroker]:
    """一道接上内存 broker 的门，邮箱按 `_MAILBOX` 建好。"""
    broker = InMemoryMessageBroker()
    await broker.ensure_queue(_MAILBOX)
    return BrokerTurnGate(_MAILBOX, broker, control), broker


async def _post(broker: InMemoryMessageBroker, *contents: str) -> None:
    for content in contents:
        await broker.publish(_MAILBOX, Message(sender='user', receiver=_MAILBOX, content=content))


def _llm(agent: BaseAgent) -> ScriptedLLM:
    llm = agent.options.llm
    assert isinstance(llm, ScriptedLLM)
    return llm


def _call(call_id: str = 'c1', text: str = 'hi') -> ToolCall:
    return ToolCall(id=call_id, name='Echo', arguments=f'{{"text":"{text}"}}')


def _overflow() -> ContextOverflowError:
    return ContextOverflowError(expected='请求在窗口内', received='超窗', next_action='压缩后重试')


def _hook_recorder(junction: Junction, log: list[tuple[str, str]]) -> HookCallback:
    async def record(**_: Any) -> HookResult:
        log.append(('hook', junction.value))
        return HookResult()

    return record


def _record_lifecycle(hooks: HooksCenter, log: list[tuple[str, str]]) -> None:
    for junction in _LIFECYCLE:
        hooks.register(junction, _hook_recorder(junction, log))


def _kwarg_recorder(junction: Junction, seen: dict[str, list[tuple[str, ...]]]) -> HookCallback:
    async def record(**kwargs: Any) -> HookResult:
        seen.setdefault(junction.value, []).append(tuple(sorted(kwargs)))
        return HookResult()

    return record


def _record_kwargs(hooks: HooksCenter, seen: dict[str, list[tuple[str, ...]]]) -> None:
    for junction in Junction:
        hooks.register(junction, _kwarg_recorder(junction, seen))


async def _collect(events: Subscription) -> list[Event]:
    seen: list[Event] = []
    while True:
        try:
            seen.append(await anext(events))
        except StopAsyncIteration:
            return seen


def _roles(blocks: list[ContextBlock]) -> list[str]:
    return [block.role for block in blocks]


def _marked(ctx: Context) -> list[str | list[Any] | None]:
    return [block.content for block in ctx.current_traj if block.extra.get('llm_call_failed')]


async def test_no_tool_calls_completes_immediately():
    agent, _ = _agent([ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert ctx.current_traj[-1].content == 'done'
    assert len(_llm(agent).calls) == 1


async def test_tool_call_then_completion_takes_two_turns():
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'assistant']
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_the_loop_keeps_going_while_the_model_keeps_calling_tools():
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[_call('c1', 'a')]),
            ScriptedTurn(tool_calls=[_call('c2', 'b')]),
            ScriptedTurn(tool_calls=[_call('c3', 'c')]),
            ScriptedTurn(content='done'),
        ]
    )
    ctx = await agent.run('go')
    assert _roles(ctx.current_traj) == [
        'user',
        'assistant',
        'tool',
        'assistant',
        'tool',
        'assistant',
        'tool',
        'assistant',
    ]
    assert len(_llm(agent).calls) == 4


async def test_tool_results_are_in_the_trajectory_before_the_next_request():
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    await agent.run('go')
    assert _roles(_llm(agent).calls[1]) == ['system', 'user', 'assistant', 'tool']


async def test_a_propagating_tool_error_ends_the_agent_with_error():
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Fatal', arguments='{}')])],
        tools={'Fatal': _Fatal()},
    )
    with pytest.raises(ToolFatalError):
        await agent.run('go')
    assert agent.context.stop_reason is StopReason.ERROR


async def test_system_prompt_is_not_stored_in_trajectory():
    """SP 每轮重建并置于请求最前，但不进轨迹——否则压缩与持久化会重复携带它。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert all(block.role != 'system' for block in ctx.current_traj)


async def test_the_request_is_the_system_block_followed_by_the_whole_trajectory():
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    second = _llm(agent).calls[1]
    assert _roles(second) == ['system', 'user', 'assistant', 'tool']
    assert second[0].content == _SYSTEM_PROMPT
    assert [block.content for block in second[1:]] == [
        block.content for block in ctx.current_traj[:3]
    ]


async def test_an_empty_system_prompt_still_leads_the_request():
    """SP 为空不改变前缀形状：请求首块随配置在与不在，会让前缀有两种形态。"""
    agent, _ = _agent([ScriptedTurn(content='done')], system_prompt='')
    await agent.run('go')
    first = _llm(agent).calls[0]
    assert _roles(first) == ['system', 'user']
    assert first[0].content == ''


async def test_the_request_prefix_is_byte_identical_and_append_only_across_turns():
    """前缀缓存按请求字节前缀匹配（spec §5.2），前缀里的一切都算：SP、工具定义、以及它们与既有
    内容的相对次序。前缀被改写既不报错也不失败，只是静默多付费。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    await agent.run('go')
    first, second = _llm(agent).calls[0], _llm(agent).calls[1]
    assert first[0].role == 'system' and second[0].role == 'system'
    assert first[0].content == second[0].content
    assert second[: len(first)] == first


async def test_tool_definitions_reach_every_request_unchanged_and_in_a_stable_key_order():
    """`==` 对 dict 键序完全无感，而键序漂移会让整条会话的前缀缓存静默失效。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    await agent.run('go')
    for kwargs in _llm(agent).call_kwargs:
        assert list(kwargs) == ['tools']
        assert kwargs['tools'] == _TOOL_SCHEMAS
        assert [list(schema) for schema in kwargs['tools']] == [
            list(schema) for schema in _TOOL_SCHEMAS
        ]
        assert [list(schema['parameters']) for schema in kwargs['tools']] == [
            list(schema['parameters']) for schema in _TOOL_SCHEMAS
        ]
        assert [list(schema['parameters']['properties']) for schema in kwargs['tools']] == [
            ['text'],
            ['left', 'right'],
            ['text'],
        ]


async def test_the_tool_definitions_reach_the_request_in_registration_order():
    """次序进请求字节前缀：重排既不报错也不失败，只让整条会话的缓存静默失效。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    await agent.run('go')
    for kwargs in _llm(agent).call_kwargs:
        assert [schema['name'] for schema in kwargs['tools']] == ['Echo', 'Concat', 'Upper']


async def test_every_registered_tool_reaches_the_request_exactly_once():
    agent, _ = _agent([ScriptedTurn(content='done')])
    await agent.run('go')
    names = [schema['name'] for schema in _llm(agent).call_kwargs[0]['tools']]
    assert len(names) == len(agent.options.tools) == 3


async def test_an_agent_without_tools_sends_an_empty_tool_list():
    agent, _ = _agent([ScriptedTurn(content='done')], tools={})
    await agent.run('go')
    assert _llm(agent).call_kwargs[0] == {'tools': []}


async def test_max_turns_stops_the_loop():
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[_call(f'c{index}')]) for index in range(5)],
        max_turns=2,
    )
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert len(_llm(agent).calls) == 2


@pytest.mark.parametrize('max_turns', [0, -1], ids=['zero', 'negative'])
async def test_a_budget_with_no_turns_left_stops_before_calling_the_llm(max_turns):
    """0 与「已用满」是同一件事：预算算出来就是 0 时，立刻以 MAX_TURNS 停下才是它的自然终态。"""
    agent, _ = _agent([ScriptedTurn(content='done')], max_turns=max_turns)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert _llm(agent).calls == []
    assert _roles(ctx.current_traj) == ['user']


async def test_a_failed_turn_still_consumes_the_budget():
    """翻转它，一个每轮都失败的 Agent 可以被无限次 run 而永不撞 max_turns——而默认预算是唯一挡住
    失控烧钱的东西。"""
    agent, _ = _agent(
        [ScriptedTurn(raises=RuntimeError('flaky')) for _ in range(3)],
        max_turns=2,
    )
    for query in ('one', 'two'):
        with pytest.raises(RuntimeError):
            await agent.run(query)
    exhausted = await agent.run('three')
    assert exhausted.stop_reason is StopReason.MAX_TURNS
    assert len(_llm(agent).calls) == 2


async def test_max_turns_none_runs_past_the_default_cap():
    script = [ScriptedTurn(tool_calls=[_call(f'c{index}')]) for index in range(50)]
    script.append(ScriptedTurn(content='done'))
    agent, _ = _agent(script, max_turns=None)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert len(_llm(agent).calls) == 51


def test_the_options_defaults_are_no_tools_no_system_prompt_and_a_bounded_budget():
    options = AgentOptions(name='Worker', llm=ScriptedLLM([]))
    assert (options.tools, options.system_prompt, options.max_turns) == ({}, '', 50)


def test_the_options_field_order_is_part_of_the_public_api():
    """宿主按位置构造 options 时，插一个字段进中间就换掉了它每一个实参的含义。"""
    assert tuple(field.name for field in dataclasses.fields(AgentOptions)) == (
        'name',
        'llm',
        'tools',
        'system_prompt',
        'max_turns',
        'danger_judge',
        'danger_policy',
        'max_tool_concurrency',
    )


async def _commands_that_ran(**danger: Any) -> list[str]:
    """让 Agent 跑一轮 `sudo rm -rf /`，交回工具真正执行过的命令。"""
    tool = _Runner()
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM(
            [
                ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Runner', arguments=_DANGEROUS)]),
                ScriptedTurn(content='done'),
            ]
        ),
        tools={'Runner': tool},
        **danger,
    )
    await BaseAgent(options, hooks=HooksCenter(), events=EventBus()).run('go')
    return tool.executed


async def test_an_agent_that_declared_no_danger_options_still_has_the_kernel_gate():
    """两个字段默认 None 表示「用内核默认」，不是「不装闸」。"""
    options = AgentOptions(name='Worker', llm=ScriptedLLM([]))
    assert (options.danger_judge, options.danger_policy) == (None, None)
    assert await _commands_that_ran() == []


async def test_a_host_policy_declared_on_the_options_reaches_the_executor():
    """宿主换了处置却不生效，是「配了没生效」那类缺陷——闸照常拦，而宿主以为自己关掉了它。"""
    assert await _commands_that_ran(danger_policy=AllowAll()) == ['sudo rm -rf /']


async def test_a_host_judge_declared_on_the_options_reaches_the_executor():
    """换判定与换处置是两件事：空规则表是「我自己在别处做了闸」。"""
    assert await _commands_that_ran(danger_judge=DangerJudge(())) == ['sudo rm -rf /']


@pytest.mark.parametrize(
    'danger',
    [{'danger_policy': object()}, {'danger_policy': AllowAll}, {'danger_judge': object()}],
    ids=['policy-not-a-policy', 'policy-is-the-class', 'judge-not-a-judge'],
)
def test_the_options_refuse_a_danger_field_the_executor_could_not_call(danger):
    """校验在声明的那一行，与 `validate_tool_names` 同一个理由：不校验时错误在第一次工具调用上
    现身，还指着工具（「中止于 AttributeError…是否已产生副作用未知」），而错在 options 这一行。"""
    with pytest.raises(ConfigError) as excinfo:
        AgentOptions(name='Worker', llm=ScriptedLLM([]), **danger)
    assert next(iter(danger)) in excinfo.value.expected


async def _peak_concurrency(**options: Any) -> int:
    """让 Agent 跑一轮三个可并发的调用，交回同时在跑的最大数。"""
    probe = _Probe()
    calls = [
        ToolCall(id=f'c{index}', name='Probe', arguments=f'{{"label":"{label}"}}')
        for index, label in enumerate(('beta', 'alpha', 'gamma'), start=1)
    ]
    agent_options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM([ScriptedTurn(tool_calls=calls), ScriptedTurn(content='done')]),
        tools={'Probe': probe},
        **options,
    )
    await BaseAgent(agent_options, hooks=HooksCenter(), events=EventBus()).run('go')
    return probe.peak


async def test_an_agent_that_declared_no_tool_concurrency_still_runs_a_safe_batch_together():
    options = AgentOptions(name='Worker', llm=ScriptedLLM([]))
    assert options.max_tool_concurrency == DEFAULT_MAX_TOOL_CONCURRENCY
    assert await _peak_concurrency() == 3


async def test_a_tool_concurrency_declared_on_the_options_reaches_the_executor():
    """「配了没生效」那类缺陷：宿主把并发关成串行，而工具照样并发跑，且没有任何提示。"""
    assert await _peak_concurrency(max_tool_concurrency=1) == 1


@pytest.mark.parametrize(
    'limit',
    [0, -1, True, '4', 2.5, 4.0, float('inf')],
    ids=['zero', 'negative', 'bool', 'string', 'fraction', 'whole-float', 'infinity'],
)
def test_the_options_refuse_a_tool_concurrency_the_executor_could_not_use(limit):
    """报出的名字必须是宿主写下的那个：executor 那道校验说的是 `max_concurrency`，指着一个宿主
    没写过的参数等于让它去猜。"""
    with pytest.raises(ConfigError) as excinfo:
        AgentOptions(name='Worker', llm=ScriptedLLM([]), max_tool_concurrency=limit)
    assert 'max_tool_concurrency' in excinfo.value.expected
    assert 'max_concurrency' not in excinfo.value.expected
    assert 'max_tool_concurrency' in excinfo.value.next_action


async def test_an_agent_that_declared_no_budget_still_stops_and_sends_an_empty_system_prompt():
    """默认预算是唯一挡住失控 Agent 的东西，而它失效时不报错，只是一直烧钱。"""
    llm = ScriptedLLM([ScriptedTurn(tool_calls=[_call(f'c{index}')]) for index in range(50)])
    options = AgentOptions(name='Worker', llm=llm, tools=_tools())
    agent = BaseAgent(options, hooks=HooksCenter(), events=EventBus())
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert len(llm.calls) == 50
    assert llm.calls[0][0].content == ''


async def test_each_agent_gets_its_own_id_so_a_shared_stream_can_tell_them_apart():
    bus = EventBus()
    first, _ = _agent([ScriptedTurn(content='done')], events=bus)
    second, _ = _agent([ScriptedTurn(content='done')], events=bus)
    assert first.agent_id != second.agent_id
    async with bus.subscribe() as events:
        await first.run('go')
        await second.run('go')
        await bus.close()
        delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    ids = [event.agent_id for event in delivered]
    assert ids == [first.agent_id] * 4 + [second.agent_id] * 4


def test_the_agent_id_is_a_full_uuid4_hex_string():
    """id 撞车不是软失败：AgentRegistry.register 拒绝重复 id，撞上的 Agent 直接注册不进去。
    截短它换来的可读性没有任何调用方需要。"""
    ids = {_agent([])[0].agent_id for _ in range(64)}
    assert len(ids) == 64
    assert all(len(agent_id) == 32 for agent_id in ids)
    assert all(set(agent_id) <= set('0123456789abcdef') for agent_id in ids)


async def test_run_returns_the_agents_own_context():
    agent, _ = _agent([ScriptedTurn(content='done')])
    assert await agent.run('go') is agent.context


async def test_a_second_run_continues_the_same_trajectory():
    """ZOMBIE 保持可寻址正是为了「一条消息复活、续用原上下文」（spec §4.1）。"""
    agent, _ = _agent([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    first = await agent.run('one')
    second = await agent.run('two')
    assert second is first
    assert [(block.role, block.content) for block in second.current_traj] == [
        ('user', 'one'),
        ('assistant', 'first'),
        ('user', 'two'),
        ('assistant', 'second'),
    ]
    assert _roles(_llm(agent).calls[1]) == ['system', 'user', 'assistant', 'user']


async def test_the_turn_counter_spans_runs_so_a_later_run_can_exhaust_the_budget():
    agent, hooks = _agent(
        [
            ScriptedTurn(tool_calls=[_call()]),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='again'),
        ],
        max_turns=3,
    )
    turns: list[int] = []

    async def record(**kwargs: Any) -> HookResult:
        turns.append(kwargs['turn'])
        return HookResult()

    hooks.register(Junction.TURN_START, record)
    await agent.run('one')
    assert (await agent.run('two')).stop_reason is StopReason.COMPLETED
    exhausted = await agent.run('three')
    assert turns == [1, 2, 3]
    assert exhausted.stop_reason is StopReason.MAX_TURNS
    assert len(_llm(agent).calls) == 3
    assert exhausted.current_traj[-1].content == 'three'


async def test_a_second_run_clears_the_previous_stop_reason():
    agent, _ = _agent([ScriptedTurn(content='done'), ScriptedTurn(raises=asyncio.CancelledError())])
    await agent.run('one')
    with pytest.raises(asyncio.CancelledError):
        await agent.run('two')
    assert agent.context.stop_reason is None


async def test_a_second_run_while_the_first_is_still_running_is_rejected():
    """交错写同一份轨迹会把工具结果推到另一个 run 的 user block 后面，配对的相邻性断裂；两次调用
    都正常返回 COMPLETED，而 Context 归 Agent 所有，坏前缀此后每次请求都带着走。"""
    entered, release = asyncio.Event(), asyncio.Event()
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Blocking', arguments='{}')]),
            ScriptedTurn(content='done'),
        ],
        tools={'Blocking': _Blocking(entered, release)},
    )
    task = asyncio.create_task(agent.run('go'))
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    with pytest.raises(ContractViolation):
        await agent.run('other')
    release.set()
    ctx = await asyncio.wait_for(task, timeout=_TIMEOUT)
    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'assistant']
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == ['go']


async def test_two_concurrent_runs_leave_exactly_one_of_them_unstarted():
    entered, release = asyncio.Event(), asyncio.Event()
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Blocking', arguments='{}')]),
            ScriptedTurn(content='done'),
        ],
        tools={'Blocking': _Blocking(entered, release)},
    )
    first = asyncio.create_task(agent.run('a'))
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    second = asyncio.create_task(agent.run('b'))
    release.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=_TIMEOUT
    )
    assert outcomes[0] is agent.context
    assert isinstance(outcomes[1], ContractViolation)
    assert _roles(agent.context.current_traj) == ['user', 'assistant', 'tool', 'assistant']


async def test_a_reentrant_run_from_inside_a_hook_is_rejected():
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def reenter(**_: Any) -> HookResult:
        await agent.run('nested')
        return HookResult()

    hooks.register(Junction.PRE_LLM_REQUEST, reenter)
    with pytest.raises(ContractViolation):
        await agent.run('go')
    assert _roles(agent.context.current_traj) == ['user']
    assert _llm(agent).calls == []


@pytest.mark.parametrize(
    'interrupt',
    [RuntimeError('flaky'), asyncio.CancelledError()],
    ids=['failure', 'cancelled'],
)
async def test_the_concurrency_guard_is_released_when_a_run_does_not_return_normally(interrupt):
    """守卫漏放一次就把这个实例永久锁死，而 ZOMBIE 留在注册表可寻址正是为了被 resume。"""
    agent, _ = _agent([ScriptedTurn(raises=interrupt), ScriptedTurn(content='done')])
    with pytest.raises(type(interrupt)):
        await agent.run('one')
    assert (await agent.run('two')).stop_reason is StopReason.COMPLETED


async def test_a_resume_repairs_only_the_tool_calls_the_cancellation_left_unpaired():
    """provider 会拒掉一个声明了 N 个调用、后面不足 N 条结果的 assistant 消息，而 ToolExecutor
    按约定不在取消展开的路上写轨迹。本类是这份 Context 此后唯一的持有者，修复只能在这里做。"""
    entered, never = asyncio.Event(), asyncio.Event()
    agent, _ = _agent(
        [
            ScriptedTurn(
                tool_calls=[_call('c1', 'hi'), ToolCall(id='b1', name='Blocking', arguments='{}')]
            ),
            ScriptedTurn(content='done'),
        ],
        tools={'Echo': _Echo(), 'Blocking': _Blocking(entered, never)},
    )
    task = asyncio.create_task(agent.run('go'))
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_TIMEOUT)
    assert [(block.role, block.tool_call_id) for block in agent.context.current_traj] == [
        ('user', None),
        ('assistant', None),
        ('tool', 'c1'),
    ]

    ctx = await agent.run('again')
    assert [(block.role, block.tool_call_id) for block in ctx.current_traj] == [
        ('user', None),
        ('assistant', None),
        ('tool', 'c1'),
        ('tool', 'b1'),
        ('user', None),
        ('assistant', None),
    ]
    assert _roles(_llm(agent).calls[1]) == ['system', 'user', 'assistant', 'tool', 'tool', 'user']
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_the_repaired_block_records_the_interruption_without_inventing_a_result():
    """两个方向都是对 resume 撒谎：声称没有副作用会让它跳过重做，声称成功会让它接着往下走。"""
    entered, never = asyncio.Event(), asyncio.Event()
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='b1', name='Blocking', arguments='{}')]),
            ScriptedTurn(content='done'),
        ],
        tools={'Blocking': _Blocking(entered, never)},
    )
    task = asyncio.create_task(agent.run('go'))
    await asyncio.wait_for(entered.wait(), timeout=_TIMEOUT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_TIMEOUT)
    repaired = (await agent.run('again')).current_traj[2]
    assert repaired.extra == {'interrupted': True}
    assert isinstance(repaired.content, str) and repaired.content


async def test_a_resume_leaves_an_already_paired_batch_alone():
    """工具跑完之后才失败的那一轮留下的轨迹是自洽的：这里补一块就是凭空多出一条工具结果。"""
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[_call()]),
            ScriptedTurn(raises=RuntimeError('flaky')),
            ScriptedTurn(content='done'),
        ]
    )
    with pytest.raises(RuntimeError):
        await agent.run('one')
    assert _roles(agent.context.current_traj) == ['user', 'assistant', 'tool']
    ctx = await agent.run('two')
    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'user', 'assistant']


@pytest.mark.parametrize(
    'interrupt',
    [asyncio.CancelledError(), KeyboardInterrupt()],
    ids=['cancelled', 'keyboard-interrupt'],
)
async def test_an_interrupt_is_reported_as_neither_completion_nor_failure(interrupt):
    """取消既不是正常结束也不是失败：展开的路上不得再触发切点或发事件，否则一个被杀掉的 Agent
    会留下一次干净收尾的记录。"""
    log: list[tuple[str, str]] = []
    agent, hooks = _agent([ScriptedTurn(raises=interrupt)], events=_RecordingBus(log))
    _record_lifecycle(hooks, log)
    with pytest.raises(type(interrupt)) as excinfo:
        await agent.run('go')
    assert excinfo.value is interrupt
    assert log == [
        ('event', 'agent_start'),
        ('hook', 'agent_start'),
        ('event', 'turn_start'),
        ('hook', 'turn_start'),
    ]
    assert agent.context.stop_reason is None


async def test_hooks_and_events_interleave_in_a_fixed_order():
    """事件在外、切点在内：turn 的可观测数据在 turn_end 切点之后才冻结，切点自身的耗时也计入
    （spec §5.1）。"""
    log: list[tuple[str, str]] = []
    agent, hooks = _agent([ScriptedTurn(content='done')], events=_RecordingBus(log))
    _record_lifecycle(hooks, log)
    await agent.run('go')
    assert log == [
        ('event', 'agent_start'),
        ('hook', 'agent_start'),
        ('event', 'turn_start'),
        ('hook', 'turn_start'),
        ('hook', 'turn_end'),
        ('event', 'turn_end'),
        ('hook', 'agent_end'),
        ('event', 'agent_end'),
    ]


async def test_a_failing_turn_reports_agent_error_and_never_agent_end():
    log: list[tuple[str, str]] = []
    agent, hooks = _agent([ScriptedTurn(content=None)], events=_RecordingBus(log))
    _record_lifecycle(hooks, log)
    with pytest.raises(LLMResponseError):
        await agent.run('go')
    assert log == [
        ('event', 'agent_start'),
        ('hook', 'agent_start'),
        ('event', 'turn_start'),
        ('hook', 'turn_start'),
        ('hook', 'agent_error'),
        ('event', 'agent_error'),
    ]
    assert agent.context.stop_reason is StopReason.ERROR


async def test_turn_events_reach_a_subscriber_in_order():
    bus = EventBus()
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')], events=bus
    )
    async with bus.subscribe() as events:
        await agent.run('go')
        await bus.close()
        delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
        assert events.dropped == 0
    assert [event.kind for event in delivered] == [
        'agent_start',
        'turn_start',
        'turn_end',
        'turn_start',
        'turn_end',
        'agent_end',
    ]
    assert {event.agent_id for event in delivered} == {agent.agent_id}


async def test_turn_events_carry_the_turn_number():
    bus = EventBus()
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')], events=bus
    )
    async with bus.subscribe() as events:
        await agent.run('go')
        await bus.close()
        delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    assert [event.payload for event in delivered if event.kind.startswith('turn')] == [
        {'turn': 1},
        {'turn': 1},
        {'turn': 2},
        {'turn': 2},
    ]


@pytest.mark.parametrize(
    ('script', 'decision', 'expected'),
    [
        ([ScriptedTurn(content='done')], None, 'completed'),
        ([ScriptedTurn(tool_calls=[_call()])], None, 'max_turns'),
        ([ScriptedTurn(content='never reached')], TurnGateDecision(abort=True), 'aborted'),
    ],
    ids=['completed', 'max-turns', 'aborted'],
)
async def test_agent_end_reports_the_stop_reason_as_plain_data(script, decision, expected):
    """payload 要能同样地走进程内出口与序列化的 SSE 出口，枚举成员在后一个出口上不可序列化。

    payload 与 `ctx` 是同一个周期的两个观察者：界面读前者、程序读后者，两边给出不同答案时没有
    任何一方能发现自己读到的是错的。
    """
    bus = EventBus()
    agent, _ = _agent(
        script, events=bus, max_turns=1, gate=None if decision is None else _FixedGate(decision)
    )
    async with bus.subscribe() as events:
        ctx = await agent.run('go')
        await bus.close()
        delivered = await asyncio.wait_for(_collect(events), timeout=_TIMEOUT)
    assert delivered[-1].kind == 'agent_end'
    assert delivered[-1].payload == {'stop_reason': expected}
    assert ctx.stop_reason is not None and ctx.stop_reason.value == expected


async def test_turn_end_sees_the_tool_results_already_in_the_trajectory():
    agent, hooks = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    snapshots: list[list[str]] = []

    async def record(**kwargs: Any) -> HookResult:
        snapshots.append(_roles(kwargs['ctx'].current_traj))
        return HookResult()

    hooks.register(Junction.TURN_END, record)
    await agent.run('go')
    assert snapshots == [
        ['user', 'assistant', 'tool'],
        ['user', 'assistant', 'tool', 'assistant'],
    ]


async def test_every_junction_on_the_happy_path_receives_its_documented_kwargs():
    """kwarg 名是 hook 作者写代码的依据，计入公开 API。"""
    agent, hooks = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    seen: dict[str, list[tuple[str, ...]]] = {}
    _record_kwargs(hooks, seen)
    await agent.run('go')
    assert seen['agent_start'] == [('ctx',)]
    assert seen['turn_start'] == [('ctx', 'turn'), ('ctx', 'turn')]
    assert seen['turn_end'] == [('ctx', 'message', 'turn'), ('ctx', 'message', 'turn')]
    assert seen['agent_end'] == [('ctx',)]
    assert seen['pre_llm_request'] == [('ctx', 'request'), ('ctx', 'request')]
    assert seen['post_llm_response'] == [
        ('ctx', 'request', 'response'),
        ('ctx', 'request', 'response'),
    ]
    assert 'llm_failure' not in seen
    assert 'context_overflow' not in seen
    assert 'agent_error' not in seen


async def test_the_failure_junctions_receive_the_request_and_the_error():
    agent, hooks = _agent([ScriptedTurn(raises=_overflow()), ScriptedTurn(raises=RuntimeError())])
    seen: dict[str, list[tuple[str, ...]]] = {}
    _record_kwargs(hooks, seen)

    async def handled(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    with pytest.raises(RuntimeError):
        await agent.run('go')
    assert seen['context_overflow'] == [('ctx', 'error', 'request')]
    assert seen['llm_failure'] == [('ctx', 'error', 'request')]
    assert seen['agent_error'] == [('ctx',)]


async def test_turn_end_carries_the_assistant_message_and_none_when_the_turn_produced_nothing():
    agent, hooks = _agent([ScriptedTurn(raises=_overflow()), ScriptedTurn(content='done')])
    messages: list[Any] = []

    async def record(**kwargs: Any) -> HookResult:
        messages.append(kwargs['message'])
        return HookResult()

    async def handled(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.TURN_END, record)
    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    await agent.run('go')
    assert messages[0] is None
    assert messages[1].content == 'done'


@pytest.mark.parametrize('content', [None, ''], ids=['none', 'empty-string'])
async def test_an_empty_assistant_block_is_treated_as_llm_failure(content):
    """既无 content 又无 tool_calls 的 block 不得入 context：它会污染此后所有请求的前缀。"""
    agent, _ = _agent([ScriptedTurn(content=content)])
    with pytest.raises(LLMResponseError):
        await agent.run('go')
    assert _roles(agent.context.current_traj) == ['user']


async def test_a_response_with_tool_calls_and_no_content_is_not_empty():
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_the_empty_guard_runs_after_post_llm_response():
    """守卫坐在 hook 之前，一个负责补全内容的 hook 就永远拿不到那条空响应。"""
    agent, hooks = _agent([ScriptedTurn(content=None)])

    async def fill_in(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role='assistant', content='patched')})

    hooks.register(Junction.POST_LLM_RESPONSE, fill_in)
    ctx = await agent.run('go')
    assert ctx.current_traj[-1].content == 'patched'


@pytest.mark.parametrize('content', [None, '', []], ids=['none', 'empty-string', 'empty-list'])
async def test_a_post_llm_response_hook_emptying_the_response_is_caught_too(content):
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def strip(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role='assistant', content=content)})

    hooks.register(Junction.POST_LLM_RESPONSE, strip)
    with pytest.raises(LLMResponseError):
        await agent.run('go')
    assert _roles(agent.context.current_traj) == ['user']


@pytest.mark.parametrize('role', ['user', 'system', 'tool'], ids=['user', 'system', 'tool'])
async def test_a_post_llm_response_hook_returning_a_non_assistant_block_is_rejected(role):
    """改写块会作为模型本轮的产出进轨迹：换个 role 等于替别人伪造一条消息，而空响应守卫也只认
    assistant，放过去就等于同时绕开了它。"""
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role=role, content='smuggled')})

    hooks.register(Junction.POST_LLM_RESPONSE, rewrite)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.POST_LLM_RESPONSE.value in excinfo.value.expected
    assert _roles(agent.context.current_traj) == ['user']


@pytest.mark.parametrize('rewritten', ['done', None, 42], ids=['str', 'none', 'int'])
async def test_a_post_llm_response_hook_returning_a_non_block_is_rejected(rewritten):
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'response': rewritten})

    hooks.register(Junction.POST_LLM_RESPONSE, rewrite)
    with pytest.raises(ContractViolation):
        await agent.run('go')
    assert _roles(agent.context.current_traj) == ['user']


async def test_post_llm_response_returning_no_opinion_leaves_the_response_alone():
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def observe(**_: Any) -> HookResult:
        return HookResult()

    hooks.register(Junction.POST_LLM_RESPONSE, observe)
    ctx = await agent.run('go')
    assert ctx.current_traj[-1].content == 'done'


async def test_llm_failure_hook_can_supply_substitute_response():
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])

    async def substitute(**_: Any) -> HookResult:
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='recovered')}
        )

    hooks.register(Junction.LLM_FAILURE, substitute)
    ctx = await agent.run('go')
    assert ctx.current_traj[-1].content == 'recovered'
    assert ctx.stop_reason is StopReason.COMPLETED


async def test_a_substitute_carrying_tool_calls_keeps_the_loop_going():
    agent, hooks = _agent(
        [ScriptedTurn(raises=RuntimeError('flaky')), ScriptedTurn(content='done')]
    )

    async def substitute(**_: Any) -> HookResult:
        return HookResult(
            modified={'response': ContextBlock(role='assistant', tool_calls=[_call()])}
        )

    hooks.register(Junction.LLM_FAILURE, substitute)
    ctx = await agent.run('go')
    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'assistant']


async def test_a_substitute_still_goes_through_post_llm_response():
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])
    seen: list[Any] = []

    async def substitute(**_: Any) -> HookResult:
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='recovered')}
        )

    async def observe(**kwargs: Any) -> HookResult:
        seen.append(kwargs['response'].content)
        return HookResult()

    hooks.register(Junction.LLM_FAILURE, substitute)
    hooks.register(Junction.POST_LLM_RESPONSE, observe)
    await agent.run('go')
    assert seen == ['recovered']


@pytest.mark.parametrize(
    'substitute',
    [
        'recovered',
        42,
        ContextBlock(role='user', content='recovered'),
        ContextBlock(role='assistant'),
    ],
    ids=['str', 'int', 'user-block', 'empty-assistant-block'],
)
async def test_a_substitute_that_is_not_a_usable_assistant_block_is_rejected(substitute):
    """替身直接作为模型本轮的产出进轨迹。放过一块空的，空 block 守卫就形同虚设。"""
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])

    async def supply(**_: Any) -> HookResult:
        return HookResult(modified={'response': substitute})

    hooks.register(Junction.LLM_FAILURE, supply)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.LLM_FAILURE.value in excinfo.value.expected
    assert _roles(agent.context.current_traj) == ['user']


async def test_an_unhandled_llm_failure_propagates_the_original_error():
    error = RuntimeError('flaky')
    agent, _ = _agent([ScriptedTurn(raises=error)])
    with pytest.raises(RuntimeError) as excinfo:
        await agent.run('go')
    assert excinfo.value is error
    assert agent.context.stop_reason is StopReason.ERROR


async def test_an_unhandled_llm_failure_marks_the_last_user_or_tool_block():
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[_call(text='hi')]), ScriptedTurn(raises=RuntimeError('flaky'))]
    )
    with pytest.raises(RuntimeError):
        await agent.run('go')
    assert _marked(agent.context) == ['hi']


async def test_an_unhandled_empty_response_marks_the_last_user_or_tool_block():
    agent, _ = _agent([ScriptedTurn(content=None)])
    with pytest.raises(LLMResponseError):
        await agent.run('go')
    assert _marked(agent.context) == ['go']


async def test_a_handled_llm_failure_leaves_no_failure_mark():
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])

    async def substitute(**_: Any) -> HookResult:
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='recovered')}
        )

    hooks.register(Junction.LLM_FAILURE, substitute)
    ctx = await agent.run('go')
    assert _marked(ctx) == []


def test_the_failure_mark_relies_on_current_traj_handing_out_the_live_blocks():
    """失败标记是就地改一块 `current_traj` 交出来的 block。`current_traj` 改成深拷贝，标记会写进
    一个随即被丢弃的副本——在另一个模块里静默失效。"""
    ctx = Context()
    ctx.append(ContextBlock(role='user', content='go'))
    ctx.current_traj[0].extra['probe'] = True
    assert ctx.current_traj[0].extra == {'probe': True}


async def test_context_overflow_handled_by_hook_retries_on_the_next_turn():
    agent, hooks = _agent([ScriptedTurn(raises=_overflow()), ScriptedTurn(content='done')])

    async def handled(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert len(_llm(agent).calls) == 2
    assert _roles(ctx.current_traj) == ['user', 'assistant']
    assert ctx.current_traj[-1].content == 'done'


async def test_a_handled_overflow_still_consumes_the_turn_it_happened_in():
    agent, hooks = _agent(
        [ScriptedTurn(raises=_overflow()), ScriptedTurn(content='done')], max_turns=1
    )

    async def handled(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.MAX_TURNS
    assert len(_llm(agent).calls) == 1


async def test_context_overflow_unhandled_propagates():
    error = _overflow()
    agent, _ = _agent([ScriptedTurn(raises=error)])
    with pytest.raises(ContextOverflowError) as excinfo:
        await agent.run('go')
    assert excinfo.value is error
    assert agent.context.stop_reason is StopReason.ERROR


async def test_overflow_and_generic_failures_never_reach_each_others_junction():
    """超窗是请求侧、压缩后可重试的状况；合并两条路会让压缩永不触发，只表现为任务失败。"""
    agent, hooks = _agent([ScriptedTurn(raises=_overflow()), ScriptedTurn(raises=RuntimeError())])
    fired: list[str] = []

    async def record_overflow(**_: Any) -> HookResult:
        fired.append('context_overflow')
        return HookResult(skip=True)

    async def record_failure(**_: Any) -> HookResult:
        fired.append('llm_failure')
        return HookResult()

    hooks.register(Junction.CONTEXT_OVERFLOW, record_overflow)
    hooks.register(Junction.LLM_FAILURE, record_failure)
    with pytest.raises(RuntimeError):
        await agent.run('go')
    assert fired == ['context_overflow', 'llm_failure']


async def test_pre_llm_request_hook_can_rewrite_messages():
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def inject(**kwargs: Any) -> HookResult:
        request = dict(kwargs['request'])
        request['messages'] = [*request['messages'], ContextBlock(role='user', content='injected')]
        return HookResult(modified={'request': request})

    hooks.register(Junction.PRE_LLM_REQUEST, inject)
    ctx = await agent.run('go')
    assert _llm(agent).calls[0][-1].content == 'injected'
    assert [block.content for block in ctx.current_traj] == ['go', 'done']


async def test_pre_llm_request_hook_can_add_request_kwargs():
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def tune(**kwargs: Any) -> HookResult:
        return HookResult(modified={'request': {**kwargs['request'], 'temperature': 0.0}})

    hooks.register(Junction.PRE_LLM_REQUEST, tune)
    await agent.run('go')
    assert _llm(agent).call_kwargs[0] == {'tools': _TOOL_SCHEMAS, 'temperature': 0.0}


@pytest.mark.parametrize(
    'rewritten',
    ['messages', None, [], {'tools': []}],
    ids=['str', 'none', 'list', 'dict-without-messages'],
)
async def test_a_pre_llm_request_hook_replacing_the_request_with_something_unusable_is_rejected(
    rewritten,
):
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def rewrite(**_: Any) -> HookResult:
        return HookResult(modified={'request': rewritten})

    hooks.register(Junction.PRE_LLM_REQUEST, rewrite)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.PRE_LLM_REQUEST.value in excinfo.value.expected
    assert _llm(agent).calls == []


async def test_pre_llm_request_skip_supplies_the_response_without_calling_the_model():
    """空脚本：模型只要被调用一次，ScriptedLLM 就会当场报配置错误。响应缓存与预算否决走这条路，
    静默照调等于把 hook 作者的否决扔了。"""
    agent, hooks = _agent([])

    async def cached(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ContextBlock(role='assistant', content='cached'))

    hooks.register(Junction.PRE_LLM_REQUEST, cached)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED
    assert _roles(ctx.current_traj) == ['user', 'assistant']
    assert ctx.current_traj[-1].content == 'cached'
    assert _llm(agent).calls == []


async def test_a_skipped_response_still_goes_through_post_llm_response():
    agent, hooks = _agent([])
    seen: list[Any] = []

    async def cached(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ContextBlock(role='assistant', content='cached'))

    async def observe(**kwargs: Any) -> HookResult:
        seen.append(kwargs['response'].content)
        return HookResult()

    hooks.register(Junction.PRE_LLM_REQUEST, cached)
    hooks.register(Junction.POST_LLM_RESPONSE, observe)
    await agent.run('go')
    assert seen == ['cached']


async def test_a_skipped_response_carrying_tool_calls_keeps_the_loop_going():
    agent, hooks = _agent([ScriptedTurn(content='done')])
    skipped: list[int] = []

    async def cached(**_: Any) -> HookResult:
        if skipped:
            return HookResult()
        skipped.append(1)
        return HookResult(skip=True, result=ContextBlock(role='assistant', tool_calls=[_call()]))

    hooks.register(Junction.PRE_LLM_REQUEST, cached)
    ctx = await agent.run('go')
    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'assistant']
    assert len(_llm(agent).calls) == 1


async def test_an_empty_skipped_response_goes_through_the_empty_response_guard():
    """与真实的空响应同路：空块守卫不因为响应来自 hook 而放行。"""
    agent, hooks = _agent([])

    async def cached(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ContextBlock(role='assistant'))

    hooks.register(Junction.PRE_LLM_REQUEST, cached)
    with pytest.raises(LLMResponseError):
        await agent.run('go')
    assert _roles(agent.context.current_traj) == ['user']


@pytest.mark.parametrize(
    'result',
    [None, 'cached', 42, ContextBlock(role='user', content='cached')],
    ids=['none', 'str', 'int', 'user-block'],
)
async def test_a_skipping_pre_llm_request_hook_without_an_assistant_block_is_rejected(result):
    """跳过之后没有任何东西能顶替本轮的产出，所以缺一块合法响应不能静默降级成「照调模型」。"""
    agent, hooks = _agent([])

    async def veto(**_: Any) -> HookResult:
        return HookResult(skip=True, result=result)

    hooks.register(Junction.PRE_LLM_REQUEST, veto)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.PRE_LLM_REQUEST.value in excinfo.value.expected
    assert _roles(agent.context.current_traj) == ['user']
    assert _llm(agent).calls == []


async def test_an_overflow_raised_by_a_pre_llm_request_hook_is_not_classified_as_overflow():
    """三级分类的对象是「调用 LLM」（spec §5.1 第 8 步）。那个 hook 手里就握着 request，要压缩就地
    压缩即可；把它抛的异常也收进分类，等于让压缩链在自己出错时递归调用自己。"""
    error = _overflow()
    agent, hooks = _agent([ScriptedTurn(content='done')])
    fired: list[str] = []

    async def raise_overflow(**_: Any) -> HookResult:
        raise error

    async def handled(**_: Any) -> HookResult:
        fired.append('context_overflow')
        return HookResult(skip=True)

    hooks.register(Junction.PRE_LLM_REQUEST, raise_overflow)
    hooks.register(Junction.CONTEXT_OVERFLOW, handled)
    with pytest.raises(ContextOverflowError) as excinfo:
        await agent.run('go')
    assert excinfo.value is error
    assert fired == []
    assert agent.context.stop_reason is StopReason.ERROR
    assert _llm(agent).calls == []


async def test_the_executor_looks_tools_up_at_call_time_rather_than_snapshotting_them():
    """ToolExecutor 的契约是每次调用时查表；这里传一份快照进去，那条跨模块性质会静默失效。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')], tools={})
    agent.options.tools['Echo'] = _Echo()
    ctx = await agent.run('go')
    assert ctx.current_traj[2].content == 'hi'


async def test_the_system_prompt_is_rebuilt_every_turn():
    """get_system_prompt 是被指定的公开方法：子类覆写它时，每 run 建一次与每轮建一次行为分叉。"""
    options = AgentOptions(
        name='Worker',
        llm=ScriptedLLM([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')]),
        tools=_tools(),
    )
    agent = _NumberedPrompt(options, hooks=HooksCenter(), events=EventBus())
    await agent.run('go')
    assert [call[0].content for call in _llm(agent).calls] == ['prompt 1', 'prompt 2']
    assert agent.prompt_calls == 2


async def test_the_last_turn_end_sees_the_stop_reason_already_set():
    """turn 的可观测数据在 turn_end 之后才冻结（spec §5.1）：按轮汇总的插件在最后一轮要么看到终
    态、要么看到 None，两者不能都算对。"""
    agent, hooks = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])
    seen: list[StopReason | None] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append(kwargs['ctx'].stop_reason)
        return HookResult()

    hooks.register(Junction.TURN_END, record)
    await agent.run('go')
    assert seen == [None, StopReason.COMPLETED]


async def test_agent_start_sees_the_new_query_already_in_the_trajectory():
    agent, hooks = _agent([ScriptedTurn(content='first'), ScriptedTurn(content='second')])
    snapshots: list[list[Any]] = []

    async def record(**kwargs: Any) -> HookResult:
        snapshots.append([block.content for block in kwargs['ctx'].current_traj])
        return HookResult()

    hooks.register(Junction.AGENT_START, record)
    await agent.run('one')
    await agent.run('two')
    assert snapshots == [['one'], ['one', 'first', 'two']]


async def test_llm_failure_fires_twice_when_post_llm_response_empties_the_substitute():
    """一轮内两次不同的失败：数重试次数的插件按轮计数会偏。"""
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('flaky'))])
    errors: list[str] = []

    async def substitute(**kwargs: Any) -> HookResult:
        errors.append(type(kwargs['error']).__name__)
        return HookResult(
            modified={'response': ContextBlock(role='assistant', content='recovered')}
        )

    async def strip(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role='assistant')})

    hooks.register(Junction.LLM_FAILURE, substitute)
    hooks.register(Junction.POST_LLM_RESPONSE, strip)
    ctx = await agent.run('go')
    assert errors == ['RuntimeError', 'LLMResponseError']
    assert ctx.current_traj[-1].content == 'recovered'


def test_agent_options_reject_a_tools_key_that_disagrees_with_the_tool_name():
    """宿主在声明的那一行就该拿到错误：`{'greet': Greeter()}` 建出来的 Agent，模型的每一次调用都
    落空，而 run 照常返回 COMPLETED。"""
    with pytest.raises(ConfigError) as excinfo:
        AgentOptions(name='Worker', llm=ScriptedLLM([]), tools={'echo': _Echo()})
    assert "'echo'" in excinfo.value.received
    assert "'Echo'" in excinfo.value.received


async def test_a_tool_smuggled_into_the_live_tools_table_fails_the_run_instead_of_completing():
    """`options.tools` 与 executor 共享同一个 dict（热插拔靠的就是这个），所以构造时的校验挡不住
    之后塞进来的错配条目。它必须让 run 以异常结束——把「每次调用都失败」报成 COMPLETED 是本仓最坏
    的失败形态。"""
    agent, _ = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')], tools={})
    agent.options.tools['echo'] = _Echo()
    with pytest.raises(ConfigError):
        await agent.run('go')
    assert agent.context.stop_reason is StopReason.ERROR


async def test_a_pre_llm_request_hook_cannot_rewrite_history_through_the_request():
    """这个切点上最自然的插件是「发请求前脱敏」。请求消息与轨迹块共享对象时它会永久改写历史：前缀
    此后不再逐轮一致，缓存静默全失效（spec §5.2 纪律 3），而脱敏本该发生在写入持久化之前。"""
    agent, hooks = _agent([ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')])

    async def redact(**kwargs: Any) -> HookResult:
        for block in kwargs['request']['messages']:
            block.content = '[REDACTED]'
            block.extra['redacted'] = True
            block.tool_calls.clear()
        return HookResult()

    hooks.register(Junction.PRE_LLM_REQUEST, redact)
    ctx = await agent.run('go')
    blocks = ctx.current_traj
    assert [block.content for block in blocks] == ['go', None, 'hi', 'done']
    assert [call.name for block in blocks for call in block.tool_calls] == ['Echo']
    assert [block for block in blocks if block.extra] == []


async def test_a_pre_llm_request_hooks_rewrite_still_reaches_the_model():
    """隔离不得把 hook 的改写一起隔离掉：脱敏后的那一份必须是真正发出去的那一份。"""
    agent, hooks = _agent([ScriptedTurn(content='done')])

    async def redact(**kwargs: Any) -> HookResult:
        for block in kwargs['request']['messages']:
            block.content = '[REDACTED]'
        return HookResult()

    hooks.register(Junction.PRE_LLM_REQUEST, redact)
    await agent.run('secret')
    assert [block.content for block in _llm(agent).calls[0]] == ['[REDACTED]', '[REDACTED]']


async def test_the_failure_mark_still_lands_on_the_trajectory_block_not_on_a_request_copy():
    """`_mark_failed` 与请求隔离方向相反且同样承重：它必须写到轨迹里那一块上，所以它读 ctx 而不读
    请求。把它改成读 request['messages'] 会让标记写进一份随即被丢弃的副本。"""
    agent, _ = _agent([ScriptedTurn(raises=RuntimeError('provider down'))])
    with pytest.raises(RuntimeError):
        await agent.run('go')
    assert _marked(agent.context) == ['go']


async def test_context_overflow_rejects_the_substitute_idiom_instead_of_dropping_it():
    """超窗切点只认 skip=True。收下 `modified['response']` 却不读它，作者会以为本轮已被顶上，而
    ContextOverflowError 照样上抛——两个失败切点的写法相反，猜错必须响。"""
    agent, hooks = _agent([ScriptedTurn(raises=_overflow())])

    async def substitute(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role='assistant', content='patched')})

    hooks.register(Junction.CONTEXT_OVERFLOW, substitute)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.CONTEXT_OVERFLOW.value in excinfo.value.expected
    assert Junction.LLM_FAILURE.value in excinfo.value.next_action


async def test_context_overflow_rejects_a_result_slot_alongside_skip():
    agent, hooks = _agent([ScriptedTurn(raises=_overflow())])

    async def handled_with_a_result(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ContextBlock(role='assistant', content='patched'))

    hooks.register(Junction.CONTEXT_OVERFLOW, handled_with_a_result)
    with pytest.raises(ContractViolation):
        await agent.run('go')


async def test_context_overflow_still_accepts_a_bare_skip():
    """拒绝读不到的槽位不得动到这个切点真正认的那种写法。"""
    agent, hooks = _agent([ScriptedTurn(raises=_overflow()), ScriptedTurn(content='done')])

    async def compressed(**_: Any) -> HookResult:
        return HookResult(skip=True)

    hooks.register(Junction.CONTEXT_OVERFLOW, compressed)
    ctx = await agent.run('go')
    assert ctx.stop_reason is StopReason.COMPLETED


@pytest.mark.parametrize(
    'outcome',
    [
        HookResult(skip=True),
        HookResult(skip=True, result=ContextBlock(role='assistant', content='patched')),
    ],
    ids=['skip', 'skip-with-result'],
)
async def test_llm_failure_rejects_the_skip_idiom_instead_of_dropping_it(outcome):
    """失败切点只认 `modified['response']`。静默忽略 skip 正是它自己对 pre_llm_request 的坏 result
    所拒绝的那件事：作者以为处理了，任务照样以同一个错误失败。"""
    boom = RuntimeError('provider down')
    agent, hooks = _agent([ScriptedTurn(raises=boom)])

    async def handled(**_: Any) -> HookResult:
        return outcome

    hooks.register(Junction.LLM_FAILURE, handled)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.LLM_FAILURE.value in excinfo.value.expected
    assert excinfo.value.__cause__ is boom


async def test_llm_failure_still_accepts_a_substitute_response():
    agent, hooks = _agent([ScriptedTurn(raises=RuntimeError('provider down'))])

    async def substitute(**_: Any) -> HookResult:
        return HookResult(modified={'response': ContextBlock(role='assistant', content='patched')})

    hooks.register(Junction.LLM_FAILURE, substitute)
    ctx = await agent.run('go')
    assert [block.content for block in ctx.current_traj] == ['go', 'patched']


async def test_post_llm_response_rejects_a_result_slot_instead_of_dropping_it():
    """后置切点改写产出只有 `modified['response']` 一种写法；`result` 是前置跳过时的槽位。"""
    agent, hooks = _agent([ScriptedTurn(content='original')])

    async def rewrite_via_the_wrong_slot(**_: Any) -> HookResult:
        return HookResult(skip=True, result=ContextBlock(role='assistant', content='rewritten'))

    hooks.register(Junction.POST_LLM_RESPONSE, rewrite_via_the_wrong_slot)
    with pytest.raises(ContractViolation) as excinfo:
        await agent.run('go')
    assert Junction.POST_LLM_RESPONSE.value in excinfo.value.expected


async def test_post_llm_response_still_accepts_a_bare_skip_to_end_the_chain():
    """`skip` 在后置切点上的作用是终止后续 hook，那是 HooksCenter 的语义，不是「我处理了」。"""
    agent, hooks = _agent([ScriptedTurn(content='original')])
    calls: list[str] = []

    async def stop_the_chain(**_: Any) -> HookResult:
        calls.append('first')
        return HookResult(skip=True)

    async def never(**_: Any) -> HookResult:
        calls.append('never')
        return HookResult()

    hooks.register(Junction.POST_LLM_RESPONSE, stop_the_chain)
    hooks.register(Junction.POST_LLM_RESPONSE, never)
    ctx = await agent.run('go')
    assert calls == ['first']
    assert [block.content for block in ctx.current_traj] == ['go', 'original']


async def test_gate_abort_between_turns_stops_with_aborted():
    """协作式中止落在轮与轮之间，此时轨迹是一致的——这正是 ABORTED 与硬取消的分界。"""
    control = ControlBlock()
    gate, _ = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='never reached')], gate=gate)
    control.request_abort()

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert _roles(ctx.current_traj) == ['user']
    assert _llm(agent).calls == []


async def test_an_aborted_cycle_starts_no_turn():
    """中止检查在计数与 turn_start 之前，所以被中止的那一轮从未开始：发出一个没有 turn_end 与之
    配对的 turn_start，界面上就是一轮永远做不完的工作。"""
    log: list[tuple[str, str]] = []
    control = ControlBlock()
    gate, _ = await _gate(control)
    agent, hooks = _agent(
        [ScriptedTurn(content='never reached')], gate=gate, events=_RecordingBus(log)
    )
    _record_lifecycle(hooks, log)
    control.request_abort()

    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == [
        ('event', 'agent_start'),
        ('hook', 'agent_start'),
        ('hook', 'agent_end'),
        ('event', 'agent_end'),
    ]


async def test_aborting_between_turns_does_not_spend_a_turn():
    """中止发生在计数之前：一个被中止的周期不该吃掉整个任务的预算。"""
    control = ControlBlock()
    gate, _ = await _gate(control)
    agent, _ = _agent(
        [ScriptedTurn(content='first'), ScriptedTurn(content='second')], gate=gate, max_turns=2
    )
    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)
    control.request_abort()
    await asyncio.wait_for(agent.run('again'), timeout=_TIMEOUT)
    ctx = await asyncio.wait_for(agent.run('third time'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.COMPLETED


async def test_a_gate_that_raises_ends_the_cycle_as_an_error():
    """门答不出来时替它编一个答案，等于让一次控制面故障静默变成「忽略中止、忽略插话，照常往下
    跑」；编成一次正常结束更糟——界面上这个周期看起来是干完了活。"""
    log: list[tuple[str, str]] = []
    agent, hooks = _agent(
        [ScriptedTurn(content='never reached')], gate=_BrokenGate(), events=_RecordingBus(log)
    )
    _record_lifecycle(hooks, log)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert agent.context.stop_reason is StopReason.ERROR
    assert _llm(agent).calls == []
    assert log == [
        ('event', 'agent_start'),
        ('hook', 'agent_start'),
        ('hook', 'agent_error'),
        ('event', 'agent_error'),
    ]


async def test_an_aborting_decision_wins_over_the_messages_it_carries():
    """spec §5.1 把中止检查排在消费消息之前。这一轮既然不会跑，注入只会把消息埋进一份没人再读的
    轨迹里，而它此时已经离开邮箱了。"""
    gate = _FixedGate(
        TurnGateDecision(
            abort=True,
            messages=(Message(sender='user', receiver=_MAILBOX, content='too late'),),
        )
    )
    agent, _ = _agent([ScriptedTurn(content='never reached')], gate=gate)

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert _roles(ctx.current_traj) == ['user']


async def test_the_gate_is_consulted_once_per_turn_with_that_turns_number():
    """每轮一次而不是每周期一次：门要在轮与轮之间放行中止与插话，一个周期问一次就只剩周期边界。"""
    gate = _FixedGate(TurnGateDecision())
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[_call('c1')]),
            ScriptedTurn(tool_calls=[_call('c2')]),
            ScriptedTurn(content='done'),
        ],
        gate=gate,
    )

    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert gate.turns == [1, 2, 3]


async def test_pending_messages_are_injected_before_the_turn_starts():
    """spec §5.1：消费待处理消息发生在 turn_start 之前，用户中途插话在下一轮就可见。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent(
        [ScriptedTurn(tool_calls=[_call()]), ScriptedTurn(content='done')],
        gate=gate,
    )
    await _post(broker, 'also check the logs')

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert _roles(ctx.current_traj) == ['user', 'user', 'assistant', 'tool', 'assistant']
    assert ctx.current_traj[1].content == 'also check the logs'
    assert ctx.current_traj[1].extra['sender'] == 'user'
    # 只看轨迹次序不够：一个先调模型、再把消息插进 index 1 的实现同样能通过上面三条，而它交给
    # 模型的那一轮里没有这条消息。
    assert _roles(_llm(agent).calls[0]) == ['system', 'user', 'user']


async def test_injected_messages_are_visible_to_the_turn_start_hook():
    """注入排在 turn_start 之前，挂在 turn_start 上的预算 / 审计 hook 看到的才是这一轮真正要发出
    去的轨迹。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, hooks = _agent([ScriptedTurn(content='ok')], gate=gate)
    seen: list[list[Any]] = []

    async def record(**kwargs: Any) -> HookResult:
        seen.append([block.content for block in kwargs['ctx'].current_traj])
        return HookResult()

    hooks.register(Junction.TURN_START, record)
    await _post(broker, 'also check the logs')

    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert seen == [['go', 'also check the logs']]


async def test_an_injected_message_goes_into_the_trajectory_not_into_the_system_prompt():
    """SP 是请求前缀而不是对话的一部分：把消息拼进它，该会话此后每一次请求的缓存都静默失效
    （spec §5.2）。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await _post(broker, 'also check the logs')

    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    system_block = _llm(agent).calls[0][0]
    assert system_block.role == 'system'
    assert system_block.content == _SYSTEM_PROMPT


async def test_a_message_arriving_mid_run_is_injected_before_the_next_turn():
    """门每轮问一次而不是每周期问一次：这条消息在第一轮已经跑起来之后才到，只有逐轮问才看得见。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='p1', name='Publisher', arguments='{}')]),
            ScriptedTurn(content='done'),
        ],
        tools={'Publisher': _Publisher(broker, _MAILBOX, 'and now check the logs')},
        gate=gate,
    )

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert _roles(ctx.current_traj) == ['user', 'assistant', 'tool', 'user', 'assistant']
    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'go',
        'and now check the logs',
    ]


@pytest.mark.parametrize(
    'trust', [TrustLevel.UNTRUSTED, TrustLevel.TRUSTED], ids=['untrusted', 'trusted']
)
async def test_injected_messages_keep_their_trust_level(trust):
    """标记必须随内容走（spec §12.2），而这是个两值格，两侧都得走到：只走一侧时「原样带过来」与
    「写死成这一侧」是同一个答案。写死 TRUSTED 让外部内容绕过授权升级；写死 UNTRUSTED 则让每一条
    用户插话都要求升级，而且不报错。
    """
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await broker.publish(
        _MAILBOX,
        Message(
            sender='webhook',
            receiver=_MAILBOX,
            content='<from the internet>',
            trust=trust,
        ),
    )

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    injected = [block for block in ctx.current_traj if block.role == 'user']
    assert [block.trust for block in injected] == [trust]


async def test_three_injected_messages_keep_arrival_order():
    """基数 3 且既非升序亦非降序：两条消息挡不住任何排序类变异。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await _post(broker, 'beta', 'alpha', 'gamma')

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        'beta',
        'alpha',
        'gamma',
    ]


async def test_injected_content_reaches_the_context_byte_for_byte():
    """空串是一个取值而不是一次缺席，首尾空白同理。顺手清洗（`.strip()`、真值判断）会静默改写用户
    说过的话，而那条消息此时已经离开邮箱、无从找回。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await _post(broker, '  padded  ', '', '\n\t')

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    assert [block.content for block in ctx.current_traj if block.role == 'user'] == [
        '  padded  ',
        '',
        '\n\t',
    ]


async def test_message_injected_fires_per_message_once_that_message_is_in_the_context():
    """这个切点报告的是既成事实：它在消息**已经**进上下文之后才触发，因此它改不了任何东西。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, hooks = _agent([ScriptedTurn(content='ok')], gate=gate)
    seen: list[tuple[Any, list[Any]]] = []

    async def record(**kwargs: Any) -> None:
        message = kwargs['message']
        assert isinstance(message, Message)
        seen.append((message.content, [block.content for block in kwargs['ctx'].current_traj]))

    hooks.register(Junction.MESSAGE_INJECTED, record)
    await _post(broker, 'beta', 'alpha', 'gamma')

    await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    assert seen == [
        ('beta', ['beta']),
        ('alpha', ['beta', 'alpha']),
        ('gamma', ['beta', 'alpha', 'gamma']),
    ]


async def test_the_message_injected_hook_gets_the_live_message_not_a_snapshot():
    """切点交出的是门递来的那一条消息本身。审计与 surface 据此回查它，拿到快照就等于回查到一份此
    后再也不会更新的副本。"""
    posted = [
        Message(sender='user', receiver=_MAILBOX, content=content)
        for content in ('beta', 'alpha', 'gamma')
    ]
    agent, hooks = _agent(
        [ScriptedTurn(content='ok')], gate=_FixedGate(TurnGateDecision(messages=tuple(posted)))
    )
    seen: list[Message] = []

    async def record(**kwargs: Any) -> None:
        received = kwargs['message']
        assert isinstance(received, Message)
        seen.append(received)

    hooks.register(Junction.MESSAGE_INJECTED, record)
    await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    # 改完再读同一个对象：交一份副本的实现下跨对象比较取值同样相等，只有改过之后才看得出交出的是
    # 不是那一个。逐条改成互不相同的值，一并挡住「每一条都交出同一个对象」。
    for index, message in enumerate(posted):
        message.content = f'tampered {index}'
    assert [received.content for received in seen] == ['tampered 0', 'tampered 1', 'tampered 2']


async def test_each_injected_block_records_the_id_of_its_own_message():
    """`extra` 是这一块内容的来源事实，审计与 surface 靠 message_id 把它追回那条消息；几块共用
    一份来源记录，追回去的会是同一条。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    posted = [
        Message(sender='user', receiver=_MAILBOX, content=content)
        for content in ('beta', 'alpha', 'gamma')
    ]
    for message in posted:
        await broker.publish(_MAILBOX, message)

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    injected = [block for block in ctx.current_traj if block.role == 'user']
    assert [block.extra['message_id'] for block in injected] == [message.id for message in posted]
    # 改完再读同一个对象：几块共享同一份 extra 时，跨两块比较取值仍然相等，只有改过之后才看得出
    # 它们本来就是一份。
    injected[0].extra['message_id'] = 'tampered'
    assert injected[1].extra['message_id'] == posted[1].id


async def test_the_injected_blocks_source_record_carries_the_trigger_as_plain_data():
    """`extra` 随轨迹持久化，枚举成员走不出序列化出口——与事件 payload 同一条规则。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await broker.publish(
        _MAILBOX,
        Message(
            sender='user',
            receiver=_MAILBOX,
            content='look at this once you are free',
            trigger=Trigger.DEFERRED,
        ),
    )

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    injected = [block for block in ctx.current_traj if block.role == 'user']
    assert [block.extra['trigger'] for block in injected] == ['deferred']


async def test_every_injected_blocks_source_record_lists_its_keys_in_one_fixed_order():
    """`extra` 随块进 `request['messages']`，键序漂移让整条会话的缓存静默失效（spec §5.2）——而
    `==` 比较 dict 对键序完全无感，只有 `list(d)` 看得见它。"""
    control = ControlBlock()
    gate, broker = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=gate)
    await _post(broker, 'beta', 'alpha', 'gamma')

    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)

    injected = [block for block in ctx.current_traj if block.role == 'user']
    assert [list(block.extra) for block in injected] == [
        ['message_id', 'sender', 'trigger'],
        ['message_id', 'sender', 'trigger'],
        ['message_id', 'sender', 'trigger'],
    ]


async def test_an_empty_query_is_still_a_query_and_not_the_gate_sentinel():
    """`None` 是「这一轮的输入从门里来」的哨兵，空串不是：按真值判断会把一次空提问静默变成一次
    唤醒，而模型此后看不到用户确实说过话。"""
    agent, _ = _agent([ScriptedTurn(content='ok')])

    ctx = await asyncio.wait_for(agent.run(''), timeout=_TIMEOUT)

    assert [(block.role, block.content) for block in ctx.current_traj] == [
        ('user', ''),
        ('assistant', 'ok'),
    ]


async def test_run_without_a_query_appends_no_user_block_of_its_own():
    """`None` 表示「这一轮的输入从门里来」；补一块空 user block 会让模型收到一条没有内容的消息。"""
    agent, _ = _agent([ScriptedTurn(content='ok')])
    ctx = await asyncio.wait_for(agent.run(None), timeout=_TIMEOUT)
    assert _roles(ctx.current_traj) == ['assistant']


async def test_run_defaults_to_taking_its_input_from_the_gate():
    """签名默认值与显式 None 必须同路：事件循环被一条消息唤醒时不带 query。"""
    agent, _ = _agent([ScriptedTurn(content='ok')])
    ctx = await asyncio.wait_for(agent.run(), timeout=_TIMEOUT)
    assert _roles(ctx.current_traj) == ['assistant']


async def test_a_gate_attached_after_construction_drives_the_turns():
    """`Runtime` 只能在构造之后装门：门要按 agent_id 认邮箱，而 agent_id 在构造时才生成。"""
    control = ControlBlock()
    gate, _ = await _gate(control)
    agent, _ = _agent([ScriptedTurn(content='never reached')])
    agent.attach_gate(gate)
    control.request_abort()

    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert ctx.stop_reason is StopReason.ABORTED
    assert _llm(agent).calls == []


@pytest.mark.parametrize('via_constructor', [False, True], ids=['attached', 'constructed'])
async def test_a_rejected_second_gate_leaves_the_first_one_driving_the_turns(via_constructor):
    """两道门就是两个消费同一个邮箱的人，`consume` 恰好一次，输的那个**静默**收不到消息。构造
    参数与 attach_gate 是同一件事的两个入口，守卫必须认得两者。

    抛出**之外**还要断言状态没动：捕获 `ContractViolation` 继续走的调用方（Runtime 的降级路径）
    会假定什么都没发生，而一个换完门再抛的实现此后由第二道门驱动，第一道门静默失联。
    """
    first = _FixedGate(TurnGateDecision())
    second = _FixedGate(TurnGateDecision())
    agent, _ = _agent([ScriptedTurn(content='ok')], gate=first if via_constructor else None)
    if not via_constructor:
        agent.attach_gate(first)
    with pytest.raises(ContractViolation):
        agent.attach_gate(second)

    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert (first.turns, second.turns) == ([1], [])
