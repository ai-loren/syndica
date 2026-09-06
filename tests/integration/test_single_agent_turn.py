"""端到端：本计划的交付判据。

创建一个 Agent、给它一句话，它调 LLM、执行工具、返回结果，且状态转换与事件流一并就位。

本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时是
字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每个
真实工具上必炸。
"""

from __future__ import annotations

from syndica.kernel import (
    AgentFactory,
    AgentOptions,
    AgentStatus,
    BaseAgent,
    Event,
    EventBus,
    HooksCenter,
    Runtime,
    ScriptedLLM,
    ScriptedTurn,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
)

# 工具顺序逐字进请求前缀（spec §5.2）。三个而非两个，且注册顺序既非升序（Farewell / Greeter /
# Introduce）亦非降序、反转后同样两者都不是：n <= 2 时「保持注册顺序」与「排序」「反转」里必有
# 一对是同一个答案。
_TOOL_NAMES = ['Greeter', 'Farewell', 'Introduce']


class Greeter(Tool):
    async def execute(self, name: str) -> ToolResult:
        """向 name 打招呼。"""
        return ToolResult(output=f'Hello, {name}!', meta={'greeted': name})


class Farewell(Tool):
    async def execute(self, name: str) -> ToolResult:
        """向 name 告别。"""
        return ToolResult(output=f'Bye, {name}!', meta={'left': name})


class Introduce(Tool):
    async def execute(self, name: str) -> ToolResult:
        """介绍 name。"""
        return ToolResult(output=f'This is {name}.', meta={'introduced': name})


def _build(llm: ScriptedLLM) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Greeter',
                llm=llm,
                tools={'Greeter': Greeter(), 'Farewell': Farewell(), 'Introduce': Introduce()},
                system_prompt='You greet people.',
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def test_agent_calls_tool_then_answers():
    runtime = Runtime()
    subscription = runtime.events.subscribe()
    llm = ScriptedLLM(
        [
            ScriptedTurn(
                tool_calls=[ToolCall(id='c1', name='Greeter', arguments='{"name":"Loren"}')]
            ),
            ScriptedTurn(content='I greeted Loren.'),
        ]
    )

    agent_id = await runtime.spawn(_build(llm), query='Please greet Loren')
    ctx = await runtime.wait(agent_id)

    assert ctx.stop_reason is StopReason.COMPLETED
    assert [schema['name'] for schema in llm.call_kwargs[0]['tools']] == _TOOL_NAMES
    tool_block = next(block for block in ctx.current_traj if block.role == 'tool')
    assert tool_block.content == 'Hello, Loren!'
    assert tool_block.extra['tool_meta'] == {'greeted': 'Loren'}
    assert ctx.current_traj[-1].content == 'I greeted Loren.'

    info = await runtime.find(agent_id)
    assert info is not None
    assert info.name == 'Greeter'
    assert info.status is AgentStatus.ZOMBIE

    await runtime.shutdown()

    events: list[Event] = [event async for event in subscription]
    assert [event.kind for event in events] == [
        'agent_start',
        'turn_start',
        'turn_end',
        'turn_start',
        'turn_end',
        'agent_end',
    ]
    assert {event.agent_id for event in events} == {agent_id}
    assert events[-1].payload == {'stop_reason': 'completed'}
