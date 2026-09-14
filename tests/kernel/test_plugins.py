"""本模块**必须**保留 `from __future__ import annotations`：仓库每个模块都带它，于是标注在运行时
是字符串。少了它，这里的工具类持有真类型对象，而 schema 反射按字符串取标注的缺陷在这里全绿、在每
个真实工具上必炸。
"""

from __future__ import annotations

import asyncio

import pytest

from syndica.kernel import agent as agent_module
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HookCallback, HookResult, HooksCenter, Junction, JunctionKind
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.plugins import install_plugins
from syndica.kernel.protocols.broker import MissingQueueCallback
from syndica.kernel.runtime import Runtime
from syndica.kernel.stores import InMemoryMessageBroker
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 2.0


class _CallbackCountingBroker(InMemoryMessageBroker):
    """记下谁在自己身上挂过按需激活回调。没有公开的读法，而「挂了没有」正是被测性质。"""

    def __init__(self) -> None:
        super().__init__()
        self.registered: list[MissingQueueCallback] = []

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self.registered.append(cb)
        super().on_missing_queue(cb)


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
    """spec §5.3：Agent 局部先于 Runtime 级。基数 3，且顺序既非升序亦非降序、也不等于反转、
    也不等于排序结果。"""
    log: list[str] = []
    shared = HooksCenter()
    install_plugins(shared, [_Recorder('zeta', log), _Recorder('alpha', log)])
    agent = _agent(shared)

    async def local(**_: object) -> None:
        log.append('local')

    agent.register_hook(Junction.PRE_TOOL_USE, local)
    await asyncio.wait_for(agent.run('go'), timeout=_TIMEOUT)

    assert log == ['local', 'zeta', 'alpha']


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
    """被拒的装配不在中心上留下 hook：装到一半才发现重名，宿主手里就是一个构造失败、却已经改过
    hook 中心的 Runtime，而它没有任何拆除入口。"""
    log: list[str] = []
    hooks = HooksCenter()
    with pytest.raises(ConfigError):
        install_plugins(hooks, [_Recorder('first', log), _Recorder('first', log)])
    assert hooks._collect(Junction.PRE_TOOL_USE) == []  # pyright: ignore[reportPrivateUsage]


async def test_a_plugin_whose_register_raises_leaves_no_hook_from_the_plugins_before_it():
    """`register` 是第三方代码，逐项校验判不出它会抛，所以「校验全做完才注册」这一道拦不住它。不还原
    的话，宿主手里同样是一个构造失败、却已经挂上了半套 hook 的中心，而中心没有拆除入口。"""
    log: list[str] = []
    hooks = HooksCenter()

    class _Explodes:
        name = 'boom'

        def register(self, hooks: HooksCenter) -> None:
            raise RuntimeError('third-party register failed')

    with pytest.raises(RuntimeError):
        install_plugins(
            hooks,
            [_Recorder('first', log), _Explodes(), _Recorder('third', log)],  # pyright: ignore[reportArgumentType]
        )
    await hooks.trigger(Junction.PRE_TOOL_USE, args={})
    assert log == []


async def test_a_plugin_whose_register_raises_a_base_exception_is_rolled_back_too():
    """`register` 是第三方代码，`KeyboardInterrupt` / `CancelledError` 从它里面出来，与普通异常留下
    的是同一个半装配的中心。只接 `Exception` 就把这条路放回原样。"""
    log: list[str] = []
    hooks = HooksCenter()

    class _Interrupts:
        name = 'interrupts'

        def register(self, hooks: HooksCenter) -> None:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        install_plugins(hooks, [_Recorder('first', log), _Interrupts()])  # pyright: ignore[reportArgumentType]
    await hooks.trigger(Junction.PRE_TOOL_USE, args={})
    assert log == []


def test_a_plugin_whose_register_raises_leaves_no_extension_junction_declared():
    """分类声明与 hook 同属中心的可写状态：留下一条声明，后来者的 register 会挂到一个没有触发方的
    切点上，而那正是「没声明过」这道守卫要拒的东西。"""
    hooks = HooksCenter()

    class _DeclaresThenExplodes:
        name = 'boom'

        def register(self, hooks: HooksCenter) -> None:
            hooks.declare_extension_junction('cap:event', JunctionKind.OBSERVABLE)
            raise RuntimeError('third-party register failed')

    with pytest.raises(RuntimeError):
        install_plugins(hooks, [_DeclaresThenExplodes()])  # pyright: ignore[reportArgumentType]

    async def cb(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation):
        hooks.register('cap:event', cb)


async def test_a_failed_assembly_keeps_the_extension_junctions_that_were_already_declared():
    """还原的基准是进来时的样子。把分类表整个清空同样是一次错误的还原，症状更隐蔽：此后往那个切点挂
    hook 会撞上「没声明过」，而声明它的那个能力早就装好了。"""
    hooks = HooksCenter()
    hooks.declare_extension_junction('artifact:created', JunctionKind.OBSERVABLE)
    log: list[str] = []

    class _Explodes:
        name = 'boom'

        def register(self, hooks: HooksCenter) -> None:
            raise RuntimeError('third-party register failed')

    with pytest.raises(RuntimeError):
        install_plugins(hooks, [_Explodes()])  # pyright: ignore[reportArgumentType]

    async def cb(**_: object) -> None:
        log.append('cb')

    hooks.register('artifact:created', cb)
    await hooks.trigger('artifact:created')
    assert log == ['cb']


async def test_a_failed_assembly_keeps_the_hooks_that_were_already_on_the_center():
    """还原的基准是**进来时**的样子，不是空中心：宿主可以先自己挂 hook 再装插件，把它们一起抹掉是拿
    一次失败的装配去毁一份与它无关的配置。"""
    log: list[str] = []
    hooks = HooksCenter()

    async def preexisting(**_: object) -> None:
        log.append('preexisting')

    hooks.register(Junction.PRE_TOOL_USE, preexisting)

    class _Explodes:
        name = 'boom'

        def register(self, hooks: HooksCenter) -> None:
            raise RuntimeError('third-party register failed')

    with pytest.raises(RuntimeError):
        install_plugins(hooks, [_Explodes()])  # pyright: ignore[reportArgumentType]
    await hooks.trigger(Junction.PRE_TOOL_USE, args={})
    assert log == ['preexisting']


class _DriftingName:
    """第一次读交回 `first`，此后交回别的。`name` 是 property，第二次读就是取一个未经校验的值。"""

    def __init__(self, first: str, later: object) -> None:
        self._first = first
        self._later = later
        self._reads = 0

    @property
    def name(self) -> object:
        self._reads += 1
        return self._first if self._reads == 1 else self._later

    def register(self, hooks: HooksCenter) -> None:
        return None


def test_a_plugin_name_is_read_once_so_the_snapshot_holds_the_value_that_was_checked():
    """交给宿主排障的那个名字必须是「非空 str」那道检查放过的那一个，否则快照里会躺着一个 int。"""
    plugin = _DriftingName('drifting', 7)
    assert install_plugins(HooksCenter(), [plugin]) == ('drifting',)  # pyright: ignore[reportArgumentType]


def test_a_duplicate_name_is_caught_against_the_value_that_was_checked():
    """查重也只能比对校验过的那一次读：再读一遍，一个第二次报别的名字的插件就能把真实的重名躲过去，
    而重名的后果是它的 hook 跑两遍——计数翻倍、改写叠加。"""
    log: list[str] = []
    with pytest.raises(ConfigError) as info:
        install_plugins(
            HooksCenter(),
            [_Recorder('dup', log), _DriftingName('dup', 'looks-different')],  # pyright: ignore[reportArgumentType]
        )
    assert 'dup' in info.value.received


def test_a_runtime_whose_plugins_are_refused_does_not_get_built():
    log: list[str] = []
    with pytest.raises(ConfigError):
        Runtime(plugins=[_Recorder('same', log), _Recorder('same', log)])


def test_a_runtime_whose_plugins_are_refused_leaves_no_callback_on_a_shared_broker():
    """`on_missing_queue` 是 append，而没人替一个构造失败的 Runtime 调 `off_missing_queue`：装配必须
    跑在挂回调之前，否则此后每一次「队列不存在」都会问到一条指向构造失败的 Runtime 的回调。"""
    log: list[str] = []
    broker = _CallbackCountingBroker()
    with pytest.raises(ConfigError):
        Runtime(broker=broker, plugins=[_Recorder('same', log), _Recorder('same', log)])
    assert broker.registered == []


def test_a_plugins_argument_of_zero_is_refused_rather_than_read_as_no_plugins():
    """`plugins or ()` 会把 0 / 空字符串这类 falsy 值当成「没传插件」静默收下，而它们是误传：宿主
    写下的那一行错了，报出来它才改得到。"""
    with pytest.raises(ConfigError) as info:
        Runtime(plugins=0)  # pyright: ignore[reportArgumentType]
    assert 'plugins' in info.value.expected
    assert 'int' in info.value.received


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


def test_exactly_three_junctions_are_out_of_reach_from_the_agent_local_layer():
    """这张表决定 `register_hook` 拒谁。多一个就把宿主真能用的切点挡掉，少一个就放回一个永不触发的
    hook。"""
    assert {junction.value for junction in agent_module._RUNTIME_ONLY} == {  # pyright: ignore[reportPrivateUsage]
        'runtime_shutdown',
        'agent_spawn',
        'agent_despawn',
    }


@pytest.mark.parametrize(
    'junction',
    [Junction.RUNTIME_SHUTDOWN, Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN],
    ids=['runtime-shutdown', 'agent-spawn', 'agent-despawn'],
)
def test_a_junction_only_the_runtime_triggers_is_refused_on_a_single_agent(junction: Junction):
    """正因为外层看不见内层，这三个切点只从 Runtime 的中心触发：挂在 Agent 局部层上的回调结构性
    地一次都不会跑，而 `agent_spawn` 恰好是宿主会真心想按 Agent 挂的那个（父子关系只能在它上面
    记）。"""
    agent = _agent(HooksCenter())

    async def local(**_: object) -> None:
        return None

    with pytest.raises(ContractViolation) as info:
        agent.register_hook(junction, local)
    assert junction.value in info.value.received
    assert 'runtime.hooks.register' in info.value.next_action
    assert junction.name in info.value.next_action
    # 被拒的注册不留痕迹：留下来的那个回调仍然挂在这一层上，而这次调用报的是「没挂上」。
    assert agent._hooks._collect(junction) == []  # pyright: ignore[reportPrivateUsage]


# 差集按字面枚举那三个，**不**从 `_RUNTIME_ONLY` 派生：派生的话，往那张表里多塞一个切点会让本清单
# 跟着缩水，于是「多挡了一个」这个变异恒不被抓。
_HOOKABLE_ON_AN_AGENT: list[Junction] = sorted(
    set(Junction) - {Junction.RUNTIME_SHUTDOWN, Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN},
    key=lambda junction: junction.value,
)


@pytest.mark.parametrize(
    'junction',
    _HOOKABLE_ON_AN_AGENT,
    ids=[junction.value for junction in _HOOKABLE_ON_AN_AGENT],
)
def test_every_other_junction_stays_hookable_on_a_single_agent(junction: Junction):
    """这道拒绝只该挡住结构上跑不到的那三个：多挡一个，宿主想给单个 Agent 加的增强就没有落点了。"""
    agent = _agent(HooksCenter())

    async def local(**_: object) -> None:
        return None

    agent.register_hook(junction, local)


async def test_the_three_runtime_only_junctions_do_fire_on_the_runtime_center():
    """拒绝的理由是「只从 Runtime 的中心触发」。那一侧真的会触发，否则这条诊断给的下一步也是
    空话。"""
    log: list[str] = []
    runtime = Runtime()

    def recorder(name: str) -> HookCallback:
        async def cb(**_: object) -> None:
            log.append(name)

        return cb

    for junction in (Junction.AGENT_SPAWN, Junction.AGENT_DESPAWN, Junction.RUNTIME_SHUTDOWN):
        runtime.hooks.register(junction, recorder(junction.value))
    agent_id = await runtime.spawn(
        lambda hooks, events: BaseAgent(
            AgentOptions(
                name='Worker',
                llm=ScriptedLLM([ScriptedTurn(content='done')]),
                tools={'Echo': _Echo()},
            ),
            hooks=hooks,
            events=events,
        ),
        query='go',
    )
    await asyncio.wait_for(runtime.wait(agent_id), timeout=_TIMEOUT)
    await runtime.despawn(agent_id)
    await runtime.shutdown()

    assert log == ['agent_spawn', 'agent_despawn', 'runtime_shutdown']
