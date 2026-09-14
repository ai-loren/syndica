"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每
个真实工具上必炸。
"""

from __future__ import annotations

import asyncio

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.plugins import install_plugins
from syndica.kernel.runtime import Runtime
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 2.0


class _Echo(Tool):
    __tool_command_args__ = ()

    async def execute(self, text: str) -> ToolResult:
        """回显 text。"""
        return ToolResult(output=text)


class _Recorder:
    """把自己的名字记进共享列表的插件。名字与执行顺序都是被测性质。"""

    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    @property
    def name(self) -> str:
        return self._name

    def register(self, hooks: HooksCenter) -> None:
        async def record(**_: object) -> None:
            self._log.append(self._name)

        hooks.register(Junction.PRE_TOOL_USE, record)


def _agent(hooks: HooksCenter) -> BaseAgent:
    return BaseAgent(
        AgentOptions(
            name='Worker',
            llm=ScriptedLLM(
                [
                    ScriptedTurn(
                        tool_calls=[ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')]
                    ),
                    ScriptedTurn(content='done'),
                ]
            ),
            tools={'Echo': _Echo()},
            system_prompt='SP',
        ),
        hooks=hooks,
        events=EventBus(),
    )


async def test_agent_local_hooks_run_before_runtime_level_ones():
    """spec §5.3：Agent 局部先于 Runtime 级。基数 3，且顺序既非升序亦非降序、也不等于反转。"""
    log: list[str] = []
    shared = HooksCenter()
    install_plugins(shared, [_Recorder('beta', log), _Recorder('alpha', log)])
    agent = _agent(shared)

    async def local(**_: object) -> None:
        log.append('local')

    agent.register_hook(Junction.PRE_TOOL_USE, local)
    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == ['local', 'beta', 'alpha']


async def test_a_local_skip_terminates_the_runtime_level_hooks_too():
    """两层是一条链，不是两条：skip 若只终止本层，Runtime 级的 hook 会跑在一个已被顶替的操作上。"""
    log: list[str] = []
    shared = HooksCenter()
    install_plugins(shared, [_Recorder('beta', log)])
    agent = _agent(shared)

    async def veto(**_: object) -> HookResult:
        return HookResult(skip=True, result=ToolResult(output='vetoed'))

    agent.register_hook(Junction.PRE_TOOL_USE, veto)
    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == []
    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == ['vetoed']


async def test_a_local_rewrite_reaches_the_runtime_level_hook():
    """跑两遍链的话，Runtime 级的 hook 看到的是模型发出来的原始参数，一次局部改写就白做了。"""
    seen: list[object] = []
    shared = HooksCenter()

    async def observe(**kwargs: object) -> None:
        seen.append(kwargs.get('args'))

    shared.register(Junction.PRE_TOOL_USE, observe)
    agent = _agent(shared)

    async def rewrite(**_: object) -> HookResult:
        return HookResult(modified={'args': {'text': 'rewritten'}})

    agent.register_hook(Junction.PRE_TOOL_USE, rewrite)
    ctx = await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert seen == [{'text': 'rewritten'}]
    assert [block.content for block in ctx.current_traj if block.role == 'tool'] == ['rewritten']


async def test_a_hook_registered_on_the_agent_does_not_leak_to_a_sibling():
    """本仓是多 Agent 框架：一个 Agent 的局部增强跑进另一个的链里就是串线。"""
    log: list[str] = []
    shared = HooksCenter()
    first = _agent(shared)
    second = _agent(shared)

    async def local(**_: object) -> None:
        log.append('local')

    first.register_hook(Junction.PRE_TOOL_USE, local)
    await asyncio.wait_for(second.run('go'), timeout=_TIMEOUT)

    assert log == []


async def test_a_hook_registered_on_the_agent_is_invisible_from_the_runtime_center():
    """外层不知道内层的存在：一个插件的行为不该因为某个 Agent 挂了局部钩子而改变。"""
    log: list[str] = []
    shared = HooksCenter()
    agent = _agent(shared)

    async def local(**_: object) -> None:
        log.append('local')

    agent.register_hook(Junction.PRE_TOOL_USE, local)
    await shared.trigger(Junction.PRE_TOOL_USE, args={})

    assert log == []


async def test_a_runtime_plugin_reaches_an_agent_spawned_by_that_runtime():
    """装配链要真的接上：工厂收到的是 Runtime 的中心，Agent 把它当外层。"""
    log: list[str] = []
    runtime = Runtime(plugins=[_Recorder('beta', log)])
    try:
        agent_id = await runtime.spawn(
            lambda hooks, events: BaseAgent(
                AgentOptions(
                    name='Worker',
                    llm=ScriptedLLM(
                        [
                            ScriptedTurn(
                                tool_calls=[
                                    ToolCall(id='c1', name='Echo', arguments='{"text":"hi"}')
                                ]
                            ),
                            ScriptedTurn(content='done'),
                        ]
                    ),
                    tools={'Echo': _Echo()},
                ),
                hooks=hooks,
                events=events,
            ),
            query='go',
        )
        await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    finally:
        await runtime.shutdown()
    assert log == ['beta']


async def test_plugins_run_in_the_order_they_were_given():
    """插件清单的次序是配置的一部分：可拦截切点按注册先后串行，换个次序就换了行为。"""
    log: list[str] = []
    runtime = Runtime(
        plugins=[_Recorder('beta', log), _Recorder('alpha', log), _Recorder('gamma', log)]
    )
    assert runtime.plugins == ('beta', 'alpha', 'gamma')
    await runtime.hooks.trigger(Junction.PRE_TOOL_USE, args={})
    assert log == ['beta', 'alpha', 'gamma']
    await runtime.shutdown()


async def test_a_runtime_without_plugins_reports_an_empty_list():
    runtime = Runtime()
    assert runtime.plugins == ()
    await runtime.shutdown()


async def test_the_installed_plugin_names_are_handed_out_as_an_immutable_snapshot():
    """调用方拿它去排障（装了哪些、按什么顺序）。交出内部列表，第一个读者一次 append 就改了后来
    每个读者看到的答案。"""
    log: list[str] = []
    runtime = Runtime(plugins=[_Recorder('beta', log)])
    names = runtime.plugins
    assert isinstance(names, tuple)
    assert names == ('beta',)
    assert runtime.plugins == ('beta',)
    await runtime.shutdown()


def test_installing_two_plugins_with_the_same_name_is_refused():
    """同一个能力装两遍，它的 hook 就跑两遍——计数翻倍、改写叠加，而症状不指向任何地方。"""
    log: list[str] = []
    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), [_Recorder('same', log), _Recorder('same', log)])
    assert 'same' in info.value.received


def test_a_refused_installation_registers_nothing():
    """被拒的装配必须什么都没发生：装到一半才发现重名，宿主手里就是一个构造失败、却已经改过
    hook 中心的 Runtime，而它没有任何拆除入口。"""
    log: list[str] = []
    hooks = HooksCenter()
    with pytest.raises(ConfigError):
        install_plugins(hooks, [_Recorder('first', log), _Recorder('first', log)])
    assert hooks._collect(Junction.PRE_TOOL_USE) == []  # pyright: ignore[reportPrivateUsage]


def test_a_runtime_whose_plugins_are_refused_does_not_get_built():
    log: list[str] = []
    with pytest.raises(ConfigError):
        Runtime(plugins=[_Recorder('same', log), _Recorder('same', log)])


def test_a_plugin_without_a_register_method_is_refused_with_an_actionable_message():
    class _Broken:
        name = 'broken'

    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), [_Broken()])  # pyright: ignore[reportArgumentType]
    assert 'register' in info.value.next_action
    assert 'broken' in info.value.received


@pytest.mark.parametrize(
    'name', [None, '', '   ', 7], ids=['missing', 'empty', 'blank', 'not-a-string']
)
def test_a_plugin_without_a_usable_name_is_refused(name: object):
    """查重与排障都按名字来；名字空掉，「装了哪些」这个问题就答不出来。"""

    class _Nameless:
        def register(self, hooks: HooksCenter) -> None:
            return None

    plugin = _Nameless()
    if name is not None:
        plugin.name = name  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), [plugin])  # pyright: ignore[reportArgumentType]
    assert 'name' in info.value.expected


def test_passing_the_plugin_class_instead_of_an_instance_is_refused():
    """`runtime_checkable` 的 Protocol 对类对象无感：类上 name 与 register 都取得到，于是「忘了
    加括号」会通过检查，再在 register(hooks) 把 hooks 绑到 self 上时炸在插件自己的签名上。"""

    class _Plain:
        name = 'plain'

        def register(self, hooks: HooksCenter) -> None:
            return None

    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), [_Plain])  # pyright: ignore[reportArgumentType]
    assert '_Plain' in info.value.received
    assert '括号' in info.value.next_action


@pytest.mark.parametrize('plugins', ['abc', 7], ids=['string', 'int'])
def test_a_plugins_argument_that_is_not_a_sequence_is_refused(plugins: object):
    """单个插件忘了包 list 时，逐元素迭代报出的错误指着 plugins.py 内部而不是写下它的那一行。"""
    with pytest.raises(ConfigError) as info:
        install_plugins(HooksCenter(), plugins)  # pyright: ignore[reportArgumentType]
    assert 'plugins' in info.value.expected


async def test_an_agent_can_hook_an_extension_junction_declared_on_the_runtime():
    """分类由声明者给出，声明在外层——局部注册必须能沿装配链找到它，否则 register 就会报错。"""
    shared = HooksCenter()
    shared.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    agent = _agent(shared)
    seen: list[str] = []

    async def local(**_: object) -> None:
        seen.append('local')

    agent.register_hook('artifact:created', local)
    await shared.trigger('artifact:created')

    assert seen == []


async def test_registering_on_an_undeclared_extension_junction_still_fails_loudly():
    """分层不得把「没声明过」这条守卫稀释掉：往没人声明过的切点挂 hook 只会留一个永不触发的钩子。"""
    agent = _agent(HooksCenter())

    async def local(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        agent.register_hook('artifact:created', local)
