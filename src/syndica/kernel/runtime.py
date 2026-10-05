"""Runtime：Agent 生命周期与控制面。

按操作系统进程模型设计：Runtime 类比 init 系统，Agent 类比进程。Agent 不管理自己的生命周期——
外层由本类驱动，内层只跑 LLM + 工具循环。

**驱动任务是一个事件循环，不是一次调用。** spec §4.1 把 IDLE 定义成「已注册，事件循环在等消息」，
所以一个 Agent 的常态是**等**，一条消息触发一个周期。周期与周期之间 Context 不重建、轮次不重置
（spec §5.1）——ZOMBIE 留在注册表可寻址，正是为了「一条消息就能让它带着上下文继续干」。

**只有一条消息消费路径。** 中途插话与唤醒都由同一道门（`BrokerTurnGate`）在轮开始前消费。两条
路径分开写必然分叉，而分叉出来的差异只在有人插话时才现身。

**每一处状态转换都只走 CAS，且失败都必须留痕。** `_switch` 逐个试 CAS 而不是「读一次再写」：读与
写之间的窗口正是 CAS 存在的理由。周期边界上的转换一律走 `_oscillate`（开周期是 RUNNING，停在邮箱
上报什么状态见 `_parked_status`），它的 `allowed` 覆盖控制面能在周期之间留下的**每一个**活状态
（pause / wait_for / abort 各自留下的那些），因此被拒只剩
一种解释：条目已被摘掉，或已被别人送进终态。接管失败意味着这个 Agent 已归别人所有，本进程既不跑它
也不替它清理；其余各处失败意味着循环还在跑而注册表说着另一套——丢掉这个结论就是丢掉「它为什么是
compare-and-set」，而那次真的丢失的转换零痕迹。

**留痕只有一个例外：`wait_for` 返回时那次恢复 `RUNNING` 的 CAS。** 它被拒是常态而不是异常——
`abort` 已经把它转成 `ABORTING`、`pause` 转成 `PAUSED`，而一次在挂起期间开始的等待从头到尾没写过
`WAITING`。在这里留痕会让每一次被中止或被挂起的等待都产出一条误报的记录，而真正丢失的转换由下一个
周期边界的 `_oscillate` 报出来。

**`_finish` 先摘队列再改状态。** 这两步之间落进来的 `send` 会拿到 `ok=False`（队列没了），调用方
于是收到一次诚实的失败；反过来先改状态，那条消息会成功投进一个即将消失的队列，静默丢掉。
**丢数据是静默的、灾难性的；失败是显式的、可恢复的。**

**拿过 `ok=True` 收据的输入不得随队列一起消失。** 收据的用途是让调用方分辨可重试的瞬时失败与永久
失败，而一条「投递成功了但永远不会被处理」的消息把这个能力整个抹掉。因此非 IDLE 退出模式只在
**没有未处理输入**时才结束循环——邮箱里剩下的消息与暂存的延迟消息是同一件事的两个落点。唯一处理不
了的是预算耗尽：一个没有轮次的 Agent 产不出新的一轮，再开周期只会被同一条消息反复唤醒又立刻返回，
一个不让出控制权的空转。那条路因此无条件退出，代价由 `_finish` 记成 ERROR——**丢数据可以，静默不
可以**。

**没有输入就不开周期。** 邮箱可能在「有消息」与门的 `consume` 之间被摘掉，此时周期的第一轮会发出
一个只有 system prompt 的请求，在轨迹里留下一块没有前置 user 块的 assistant 块。轨迹是要持久化的，
这块坏前缀会让此后每一次 provider 请求都非法，而故障在下一次 resume 才现身。

**取消不做收尾工作。** 驱动任务被取消时，`finally` 里既不改注册表也不摘资源：取消可能落在轮的
中间，那时轨迹里留着不配对的调用，而 spec 把「收尾」定义为一个**有状态**的动作。收尾由发起取消
的那一方在取消之外完成，因此它不必在一个正在被取消的任务里 await。`kill` 因此把收尾放进一个独立
的收割任务——它也是那条路上**唯一**保证得了归宿的地方。

**只有一条拆除顺序。** `kill` 的收尾、`despawn`、`ExitMode.EXIT` 三条路都走 `_destroy_tree` →
`_destroy`。各写一份清理迟早会在「先摘队列还是先注销」这种地方分叉，而分叉出来的差异只在并发投递
恰好落进那个窗口时才现身。分工按「删不删注册表条目」切开：`kill` 不删（送进 ZOMBIE，条目还在，
子代不会指向空洞，因此不需要级联）；`despawn` 一定删，所以**必须**级联到全部子代，子先于父。

**条目要么和它的子树一起走，要么整棵树都留着。** `AgentRegistry.unregister` 只把自己从父的
children 上摘掉、不动自己的子代，于是删掉一个有子代的父会留下一串 `parent_id` 指着不存在的条目：
`children_of` 从此永远漏掉它们，而它们自己看起来还有个父。这是数据完整性问题不是策略，所以级联
没有开关。**级联先把整棵子树钉住再动手**：边走边拆的话，中间节点一旦注销，它下面的条目就再也走不
到（`children_of` 只能从父往下问）。钉住期间 `spawn` 拒绝往树上挂新子代、`ensure_active` 拒绝复活
树里的节点——后者若放行，复活出来的驱动会在条目被摘掉时失去句柄，成为永不被取消的孤儿。

**钉住按「一次调用」计，不是进程级的一个开关。** 同一个节点可以同时落在多条在飞级联里（拆除
中途重入的 hook、两次重叠的 `despawn`、`ExitMode.EXIT` 撞上一次外部 `despawn`），而释放写在
`finally` 里、因此**一个节点都没拆就退出时照样执行**。无条件释放于是会撤掉别人的那一份：钉住撤
早了这棵树重新开放挂子代，而新子代不在别人早已冻结好的拆除名单里，它的 `parent_id` 永久指向一个
已被注销的父。

**重叠的拆除各自动手，不等对方。** 拆除的每一步都是幂等的，同时拆没有代价；而「等另一个**调用
方**」这条边可以成环——两条级联各自停在对方正拆的节点上，两次 `despawn` 永不返回，被卡住的子树
再没有任何办法拆掉（`despawn` 是唯一入口，而它自己挂在环上）。`despawn` 承诺的是**自己的后代**，
后代成树，所以往自己子树里等（等一个子代的驱动任务停下来）不会成环；等一个平级的调用方会。同一次
消失只报一次 `AGENT_DESPAWN` 靠一份共享的收据：真的把痕迹摘干净的那一次报，而收据是同步取的，
两条并发的拆除里因此只有一个取得到。

**别人的信号不是关于世界的证据。** 一个「拆完了」的完成信号说的是**持有者离开了**，不是**这个条目
已经不在了**：它被取消或抛异常时信号同样置位，而那时条目还在。收到信号就返回的调用方于是带着一个
为假的后置条件继续往上拆，把它的父注销掉，留下永久悬空的 `parent_id`，且一条日志都不留。所以这里
没有等待者：每个调用方自己动手，动手之后**复检**痕迹是否真的没了，复检不过记一条带下一步的
ERROR——**返回即意味着「它不在了」或者「有一条 ERROR 说清了它为什么还在」**。

**`_slots` 的键恒为实例表键的子集。** EXIT 把队列、注册表条目、实例表条目与 slot 一起摘掉，于是
本类持有的每一份 Context 都对应一个仍可寻址的 Agent，随时间无界增长的只有活着的 Agent 数。代价
写在 `wait` 的契约里：被 reap 的 Agent 没有 waitpid 可言。

**递归的控制面操作对被点名的那一个抛，对遍历到的子代只记日志。** 子代可能是 ZOMBIE、可能正在
被拆，为第一个冲突抛异常只会留下一棵改了一半的树，而调用方无从得知改到哪了；被点名的那一个意图
无歧义，照常抛。CAS 失败同样按这条处理。遍历（`_descendants`）广度优先且带环保护——注册表可以是
远端的，一个环会让这个公共前置永不返回。**遍历穿过本进程不持有的节点，但不作用在它身上**：树在
共享注册表里，本进程持有哪些实例与树的形状无关，在第一个外人那里截断会漏掉它下面属于我们的整片
子树。

**控制面只作用于本进程接管过的 Agent。** 状态写进共享注册表，而这里的门只按得住本进程的事件循环。
替一个接管失败的条目写下 PAUSED，真正的持有者的 `ensure_active` 从此转不回 IDLE，而投递方仍在拿
`ok=True` 的收据——一次谁都叫不醒的静默失联。

**激活只有一个入口。** 复活 ZOMBIE、重开事件循环、日后的「从落盘会话恢复」在语义上是同一件事，
因此都走 `ensure_active`；`send` 的按需激活经 broker 的 `on_missing_queue` 回到同一个方法。让每个
调用方自己「先判断再激活」会让复活有 N 份实现。**判定「它已经活着」看驱动任务，不看注册表状态**：
停在邮箱上的 Agent 是 IDLE，而 IDLE -> RUNNING 的 CAS 对第二个驱动任务照样成功，拿状态当所有权凭据
会让同一个邮箱被两道门 consume，而 consume 恰好一次——输的那道门静默收不到任何消息。

**`WAITING` 是「轮被挂起」，不是「事件循环在等」。** `wait_for` 从一次工具调用内部发起，此刻 Agent
正在一轮的中间：turn 循环挂在 `wait_for` 上、注册表说 `WAITING`，而它的邮箱仍由门在下一轮开始前
消费——被等到的那条答复由 `take_by_correlation` 从邮箱取走，因此不会再作为普通消息注入一遍。一个
Agent 同时只有一个等待者（`_Slot.waiter`），新的顶掉旧的。**只有当下那个等待者负责把状态带回
`RUNNING`**：被顶掉的那个也去恢复的话，注册表会在新的等待还挂着的时候宣布这个 Agent 在跑，而它悬在
一次等待上、只有一条匹配的答复救得回来。同理 `resume` 回到哪个状态要看在飞的等待，不只看
`in_run`。

**`shutdown` 是终态。** 关闭后的事件总线静默丢弃 emit，此后 spawn 出来的 Agent 会照常干完活却完全
不可观测，因此 spawn 在关闭后报错。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from syndica.kernel.agent import BaseAgent
from syndica.kernel.agent_types import AgentInfo, AgentStatus, ExitMode
from syndica.kernel.context import (
    Context,
    ContextBlock,
    StopReason,
    TrustLevel,
    _no_windows,  # pyright: ignore[reportPrivateUsage]
    _trailing_unpaired_batch,  # pyright: ignore[reportPrivateUsage]
    provenance_of,
    strictest_trust,
)
from syndica.kernel.control import BrokerTurnGate, ControlBlock
from syndica.kernel.danger import (
    ConfirmationHandler,
    ConfirmationOutcome,
    ConfirmationRequest,
    DangerJudge,
    DangerPolicy,
    require_own_command_args,
    validate_confirmation_handler,
)
from syndica.kernel.driving import _DRIVING_AGENT  # pyright: ignore[reportPrivateUsage]
from syndica.kernel.errors import ConfigError, ContractViolation
from syndica.kernel.events import (
    EventBus,
    _why_copying_failed,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.history_blocks import (
    IDEMPOTENCY_KEY,
    HistoryKind,
    has_block,
    signed_block,
)
from syndica.kernel.hooks import HooksCenter, Junction
from syndica.kernel.hooks.installing import (
    Installer,
    acting_as,
    current_installer,
    no_installer,
)
from syndica.kernel.ids import (
    IdSource,
    RandomIds,
    StableKey,
    assigning_agent_id,
    becoming_owner,
    entering_owner,
    leaving_owner,
    next_key,
    releasing_agent_id,
)
from syndica.kernel.isolation import ISOLATIONS, MergeReport
from syndica.kernel.local_table import LocalAgentTable
from syndica.kernel.message import DeliveryResult, Message, Trigger
from syndica.kernel.modules import (
    FlagDeclaration,
    FlagValue,
    RuntimeModule,
    _RuntimeModuleAssembly,  # pyright: ignore[reportPrivateUsage]
)
from syndica.kernel.plugins import Plugin, install_plugins
from syndica.kernel.protocols import AgentRegistry, MessageBroker, MissingQueueCallback
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.redaction import Redactor, RuleRedactor
from syndica.kernel.session_redaction import redacted_record
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus
from syndica.kernel.stores import InMemoryAgentRegistry, InMemoryMessageBroker
from syndica.kernel.tools import orchestration_tools
from syndica.kernel.trace import AttributeValue, SpanKind
from syndica.kernel.tracing import (
    Tracer,
    _Span,  # pyright: ignore[reportPrivateUsage]
    _Tracing,  # pyright: ignore[reportPrivateUsage]
    back_in_spans,
    outside_any_span,
)
from syndica.kernel.waiting import Reply, Waiter, WaitResult, WaitState, correlation_of
from syndica.kernel.workspace_router import WorkspaceRouter

if TYPE_CHECKING:
    from syndica.kernel.catalog import AgentCatalog
    from syndica.kernel.llm import LLM
    from syndica.kernel.tool import Tool
    from syndica.kernel.topology import AgentSlot, Topology

__all__ = ['AgentFactory', 'Runtime', 'validate_agent_factories']

logger = logging.getLogger(__name__)

AgentFactory = Callable[[HooksCenter, EventBus], BaseAgent]
_StoreT = TypeVar('_StoreT')

# 「这一次 send 允不允许把目标叫醒」。用 ContextVar 而不是 Runtime 上的一个字段：它的作用域是
# **这一次调用**，两个并发 send 的意图因此互不污染；放在实例上则第二个 send 会读到第一个的开关。
# 代价是 broker 必须让回调看得见调用方 publish 时的上下文；这条已经是 MessageBroker 的明文契约
# 并由契约套件强制，见 protocols/broker.py。
_ACTIVATION_ALLOWED: ContextVar[bool] = ContextVar('syndica_activation_allowed', default=True)
_SHUTTING_DOWN: ContextVar[tuple[object, ...]] = ContextVar('syndica_shutting_down', default=())


@dataclass(frozen=True)
class _SlotSpawn:
    """经拓扑槽的这一次 spawn（`_spawn_slot` 设下，`spawn` 取键时取走）。spawn 的签名计入公开面，
    槽的键不该从那里进来；取走而不是只读：spawn 里的 hook 再 spawn 的 Agent 不是这个槽的。

    `per_caller`：on-demand 槽每次要都新建一个，实例就是要它的那次调用的，键跟着调用方；singleton
    与 keyed 的实例由槽与键确定，谁先来要都是同一个——键里带上先到者，回放时换一个先到者它就换了
    id。

    `installer`：槽上那个角色登记时的身份。工厂与 `install_own_tools` 按它跑，不按触发这次寻址的
    调用方：被收紧的 Module 寻址一个宿主的槽时，那个角色自己挂的 hook 若记在 Module 头上，它写下的
    文字就会被收紧成不可信。
    """

    name: str
    key: str
    per_caller: bool
    installer: Installer | None


_SLOT_SPAWN: ContextVar[_SlotSpawn | None] = ContextVar('syndica_slot_spawn', default=None)


def _checked_ids(ids: object) -> IdSource:
    # 类本身也带着 agent_id / message_id 两个属性，`isinstance(类, IdSource)` 因此成立；放进来
    # 要等第一次 spawn 才以一个看不出原因的 TypeError 炸开。
    if isinstance(ids, type):
        raise ConfigError(
            expected='ids 是一个 IdSource 实例',
            received=f'收到类 {ids.__name__} 本身',
            next_action=f'传 {ids.__name__}(...) 构造出的实例，而不是类（漏了括号？）',
        )
    unusable = [
        name for name in ('agent_id', 'message_id') if not callable(getattr(ids, name, None))
    ]
    if unusable:
        raise ConfigError(
            expected='ids 实现 IdSource：agent_id(key) 与 message_id(key) 都是方法，交回非空字符串',
            received=f'收到 {type(ids).__name__}，{" 与 ".join(unusable)} 不存在或不可调用',
            next_action='不传就用 uuid4；回放时传一个按稳定键交回记录里那个 id 的对象',
        )
    return cast(IdSource, ids)


def _checked_tracer(tracer: object) -> Tracer:
    if isinstance(tracer, type) or not isinstance(tracer, Tracer):
        raise ConfigError(
            expected='tracer 是一个实现了 Tracer 全部同步方法的实例',
            received=f'收到 {tracer!r}（{type(tracer).__name__}）',
            next_action=(
                '传 start_span / end_span / add_event / add_edge / llm_exchange / tool_result / '
                'wait_answer 都在的实例（例如 TraceRecorder；只想覆盖其中几个就继承 NullTracer），'
                '不要传类'
            ),
        )
    return tracer


def _fresh_id(produce: Callable[[StableKey], str], key: StableKey, what: str) -> str:
    try:
        value = cast(object, produce(key))
        if inspect.isawaitable(value):
            # async def 的方法交回协程：内核不 await id 源，协程里的 id 永远取不出来。
            if inspect.iscoroutine(value):
                value.close()
            raise ContractViolation(
                expected=f'id 源的 {what}(key) 是同步方法，当场交回 id',
                received=f'对键 {key!r} 交回了一个 awaitable',
                next_action=f'把 {what} 改成普通的 def；要查的记录提前读进内存',
            )
    except ContractViolation:
        raise
    except Exception as error:
        raise ContractViolation(
            expected=f'id 源的 {what}(key) 对每个键都交回一个 id',
            received=f'对键 {key!r} 抛出 {type(error).__name__}: {error}',
            next_action=(
                '回放时这通常是这个键不在记录里：这次运行走了一条记录里没有的路（换了模型、改了'
                '工具、改了拓扑），或记录不完整。让 id 源对记录之外的键新造一个 id，或换一份对应'
                '这次运行的记录'
            ),
        ) from error
    if not isinstance(value, str) or not value:
        raise ContractViolation(
            expected=f'id 源的 {what}(key) 交回非空字符串',
            received=f'对键 {key!r} 交回 {value!r}',
            next_action='让 id 源对每个键都交回一个非空字符串；回放时交回记录里的那个 id',
        )
    return value


def _observe_shutdown_task(task: asyncio.Task[None]) -> None:
    """取回后台关机失败；后续 await 同一任务仍会得到同一个异常。"""
    try:
        failure = task.exception()
    except asyncio.CancelledError:
        logger.error(
            '期望: Runtime 的共享关机任务完成全部清理。'
            ' 实际: 关机任务自身被取消。'
            ' 下一步: 不要直接取消 runtime:shutdown 任务；取消调用方自己的 shutdown 等待即可。'
        )
        return
    if failure is not None:
        logger.error(
            '期望: Runtime Module 全部正常关闭。'
            ' 实际: 共享关机任务以 %s: %s 结束。'
            ' 下一步: 再次 await runtime.shutdown() 取得同一异常组，并修复其中点名的 Module。',
            type(failure).__name__,
            failure,
        )


# 「此刻这段代码跑在谁的事件循环里」。`_drive` 在入口无条件置位，其余一律读它。
#
# 用 ContextVar 而不是比对 `asyncio.current_task()`：一个声明了可并发的工具跑在 `gather` 建的
# 子任务里，`current_task()` 于是不再是驱动任务，而那个驱动任务正**阻塞在**这个子任务上——按任务
# 身份判「这次操作会不会等自己停下来」会静默答否，换来一次永久挂死。`asyncio.Task` 建立时复制一份
# contextvars，子任务因此读得到驱动任务写下的值。
#
# **无条件置位是承重的**：`spawn` 从驱动任务内部建子 Agent 的驱动任务，它会继承父写下的值；只靠
# `default=None` 会让子 Agent 的守卫指着它的父。反过来 `_reap` 不是任何人的事件循环（它刻意跑在
# 事件循环之外），继承来的值在那里是一句假话，所以它进门先清掉。变量本身在 `driving`：工作区路由
# 也按它分派，而路由不能 import 本模块。

# 本进程的事件循环认得的活状态：从其中任何一个都可以被它转去另一个活状态或 ZOMBIE。不在这里的
# 就是终态，本进程不得把它带回来。周期边界的 `allowed` 由本元组派生，漏一个就是「某个状态下
# 静默转不过去」。
_LIVE_STATUSES: tuple[AgentStatus, ...] = (
    AgentStatus.IDLE,
    AgentStatus.RUNNING,
    AgentStatus.PAUSED,
    AgentStatus.WAITING,
    AgentStatus.ABORTING,
)

# 挂起要求「有一个事件循环可以停下来」。这一个元组同时决定 `pause` 的早退拒绝与它那次 CAS 的
# expected 集合：拆成两份写，两份就会各自漂移，而漂移的表现是「拒绝的理由说得通、CAS 却静默转不
# 过去」，或者反过来——放行了一个转换不进去的状态，报出的却是一句关于 CAS 的话。
_PAUSABLE_STATUSES: tuple[AgentStatus, ...] = (
    AgentStatus.IDLE,
    AgentStatus.RUNNING,
    AgentStatus.WAITING,
)


def _forkable_prefix(blocks: list[ContextBlock]) -> list[ContextBlock]:
    """分叉出去的那份轨迹：末尾那个**未配对**的批次只丢掉它的 `tool_calls`。

    分叉是从一次工具调用**内部**发起的，此刻源 Agent 的轨迹末尾正是一个已声明、尚未配对的
    assistant 块——发起分叉的那次调用自己。原样拷过去，分身一进 `run` 就会把它当成「上一次被中断
    的批次」，给每个未配对的调用补一块「副作用未知」的说明：一段凭空捏造的历史。那次调用是源
    Agent 的事，分身不需要知道。

    **要丢的是 `tool_calls`，不是 `content`。** 模型最常见的输出形态是在同一块上先写下结论再发起
    分叉，整块截掉就把这份轨迹里唯一的那句结论一起删了——而 `Fork` 交给模型的 description 逐字
    写着「分身看得到你到此刻为止的轨迹，因此不必重复前提」。配对不变式只要求那些没有结果的调用
    消失。`content` 为空时那一块整个丢掉：既无 `content` 又无 `tool_calls` 的块会污染此后每一次
    请求的前缀。

    截断连带丢掉排在那个 assistant 块之后的结果块（同一批里更早的段已经落下的那些）——它们配对的
    正是被丢掉的那些调用，留着就是一串孤儿 tool 块，而 provider 会拒掉整条消息。**它们可能携带
    这份轨迹里唯一的不可信标记**，所以 `spawn` 另外把源 Agent 的溯源结论带到分身身上（见 `spawn`
    的 `query_trust` 与 `_carry_truncated_taint`）。
    """
    batch = _trailing_unpaired_batch(blocks)
    if batch is None:
        return list(blocks)
    index, _ = batch
    head = blocks[index]
    if not head.content:
        return blocks[:index]
    return [*blocks[:index], replace(head, tool_calls=[])]


def _frozen(context: Context) -> Context:
    """这一刻的轨迹与停下的原因。列表是新的、块与原件共用（同 `current_traj`）：此后追加的块不进
    这一份，对已有块的就地改写（失败标记）照样看得见。"""
    frozen = Context()
    for block in context.current_traj:
        frozen.append(block)
    frozen.stop_reason = context.stop_reason
    return frozen


def _not_a_role_table(agents: object) -> ConfigError:
    return ConfigError(
        expected='agents 是「角色名 -> 工厂」的映射：模型按名字派活，所以名字必须是键',
        received=f'收到 {type(agents).__name__} 类型的 {agents!r}，它没有 items()',
        next_action=(
            '把 Runtime(agents=...) 改成 {名字: 工厂} 的 dict。'
            '声明式清单里 `agents: [Worker, Planner]` 是一个列表，那里只有名字、没有工厂；'
            '要按名字派活就写成 `agents: {Worker: ..., Planner: ...}`'
        ),
    )


def _bad_role_name(name: object) -> ConfigError:
    return ConfigError(
        expected='角色名是非空白字符串：模型按名字派活，没有名字的角色它寻址不到',
        received=f'agents 里有一个键是 {name!r}，类型是 {type(name).__name__}',
        next_action=(
            '把 Runtime(agents=...) 里这个键改成非空白字符串。'
            '声明式清单来自 YAML / JSON，那里的键可以是任何标量，而派活按逐字节相等匹配'
        ),
    )


def _bad_agent_factory(name: str, factory: object, received: str) -> ConfigError:
    return ConfigError(
        expected=(
            f'角色 {name!r} 的取值是一个工厂：收下 (hooks, events) 两个位置参数，交回一个 BaseAgent'
        ),
        received=received,
        next_action=(
            f'把 Runtime(agents=...) 里 {name!r} 的取值改成这样的工厂'
            '（类式 Agent 要包一层：`lambda hooks, events: MyAgent(options, hooks=hooks,'
            ' events=events)`）。不校验的话，它要到某次 Delegate 真的派活时才被调用，'
            '报出的是 runtime.py 里那一行的 TypeError，指着一个没问题的地方'
        ),
    )


def validate_agent_factories(agents: Mapping[str, AgentFactory]) -> None:
    """校验角色表。收下角色表的每一处都必须调它。

    **参数标注为 `Mapping[str, AgentFactory]`，而要挡的正是违反这个标注的运行时取值**：角色表
    的现实来源是声明式清单与宿主配置，那里没有类型检查。

    **能查的只有形状，不是身份。** 「忘了加括号」传进来一个工厂生成器同样收两个位置参数、同样
    可调用，任何签名检查都放行它；抓得住它的是工厂**交回了什么**，那道检查在 `spawn` 里。
    """
    table = cast('object', agents)
    if not isinstance(table, Mapping):
        raise _not_a_role_table(table)
    for name, factory in cast('Mapping[object, object]', table).items():
        if not isinstance(name, str) or not name.strip():
            raise _bad_role_name(name)
        if not callable(factory):
            raise _bad_agent_factory(
                name, factory, f'收到 {factory!r}，类型是 {type(factory).__name__}，它不可调用'
            )
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            # 问不出签名（部分 C 实现、`__signature__` 自己抛）就不判：把「答不出来」变成拒绝会
            # 挡掉一个合法的可调用对象，而它交回什么仍由 `spawn` 那道检查兜住。
            continue
        try:
            signature.bind(None, None)
        except TypeError as exc:
            raise _bad_agent_factory(
                name, factory, f'收到 {factory!r}，它收不下 (hooks, events) 两个位置参数：{exc}'
            ) from exc


def _artifact_kinds(value: object, frozen: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """交冻结工厂时每个角色的产物种类：键是这些工厂里的角色名，值是种类名的 list / tuple。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(
            expected='artifact_kinds 是「角色名 → 产物种类的 list / tuple」的映射',
            received=f'收到 {type(value).__name__} 类型的 {value!r}',
            next_action='写成 {"Writer": ["report"]}；没有产物种类就省略这个参数',
        )
    kinds: dict[str, tuple[str, ...]] = {}
    for role, raw in cast('Mapping[object, object]', value).items():
        if not isinstance(role, str) or role not in frozen:
            raise ConfigError(
                expected=f'artifact_kinds 的键是交来的工厂里的角色名：{sorted(frozen)!r}',
                received=f'收到键 {role!r}',
                next_action='改成工厂表里的角色名，或删掉这一项',
            )
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise ConfigError(
                expected=f'artifact_kinds[{role!r}] 是种类名的 list / tuple',
                received=f'收到 {type(raw).__name__} 类型的 {raw!r}',
                next_action=f'写成 [{raw!r}]' if isinstance(raw, str) else '写成种类名的列表',
            )
        items = tuple(cast('Sequence[object]', raw))
        if not all(isinstance(item, str) for item in items):
            raise ConfigError(
                expected=f'artifact_kinds[{role!r}] 的每一项是字符串',
                received=f'收到 {items!r}',
                next_action='把每个种类写成字符串',
            )
        kinds[role] = cast('tuple[str, ...]', items)
    return kinds


def _untrusted_output_modules(value: object, installed: Sequence[str]) -> frozenset[str]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ConfigError(
            expected='untrusted_output_modules 是已装 Module 名的 list / tuple',
            received=f'收到 {value!r}，类型是 {type(value).__name__}',
            next_action=(
                f'写成 [{value!r}]；一个字符串会被逐字符当成 Module 名'
                if isinstance(value, str)
                else '传 Module 名组成的 list；没有要收紧的就省略这个参数'
            ),
        )
    names = list(cast('Sequence[object]', value))
    unknown = [name for name in names if not isinstance(name, str) or name not in installed]
    if unknown:
        raise ConfigError(
            expected=f'untrusted_output_modules 里每一项是已装的 Module 名（{list(installed)}）',
            received=f'认不出 {unknown!r}',
            next_action='改成 modules 里某个 Module 的名字；认不出的名字什么都不收紧，等于没设',
        )
    return frozenset(cast('list[str]', names))


def _module_tool_replaced(
    cls: type[BaseAgent], replaced: Sequence[str], owners: Mapping[str, str]
) -> ConfigError:
    shown = '、'.join(f'{name!r}（Module {owners[name]!r}）' for name in replaced)
    return ConfigError(
        expected=f'类式角色 {cls.__qualname__} 的 install_own_tools 不替换 Module 装的工具',
        received=f'它换掉或删掉了 {shown}',
        next_action=(
            '给自己的工具换一个名字；'
            'Module 的工具按来自哪个 Module 收紧结果，换掉之后归属就对不上了'
        ),
    )


def _agent_on_the_runtime_center(factory: AgentFactory) -> ContractViolation:
    return ContractViolation(
        expected='工厂用 spawn 交给它的 hooks 构造 Agent',
        received=(
            f'{getattr(factory, "__qualname__", repr(factory))}'
            ' 交回的 Agent 直接建在 runtime.hooks 上'
        ),
        next_action=(
            '把工厂收到的第一个参数作为 hooks 传给 Agent；'
            '要给所有 Agent 挂 hook 就用 Runtime 插件，而不是把 Agent 建在 Runtime 的中心上'
        ),
    )


def _bad_factory_return(factory: AgentFactory, produced: object) -> ContractViolation:
    return ContractViolation(
        expected='工厂交回一个 BaseAgent',
        received=(
            f'{getattr(factory, "__qualname__", repr(factory))} 交回了'
            f' {type(produced).__name__} 类型的 {produced!r}'
        ),
        next_action=(
            '这多半是「忘了加括号」：传进去的是一个**造工厂的函数**而不是工厂本身'
            '（`spawn(make_worker)` 应为 `spawn(make_worker(...))`）。'
            '它来自 Runtime(agents=...) 的角色表就改那一行。'
            '不拦的话，下一步是 agent.agent_id 撞出的 AttributeError，指着 runtime.py'
        ),
    )


def _orchestration_name_clash(name: str, clashes: list[str]) -> ConfigError:
    return ConfigError(
        expected='Runtime(orchestration=True) 装上的编排工具名与这个 Agent 自己的工具都不重名',
        received=f'agent {name!r} 自己的工具表里已经有 {clashes}',
        next_action=(
            '给自己的那个工具换个名字，或者关掉 Runtime(orchestration=True) 手工装配编排工具。'
            '静默覆盖会让它自己那一个永远调不到——模型按同一个名字发起的调用全落到编排工具上，'
            '而 run 照常以 COMPLETED 返回。本次 spawn 什么都没发生'
        ),
    )


def _cannot_fork_from(fork_from: str) -> ContractViolation:
    return ContractViolation(
        expected=f'分叉源 {fork_from!r} 的实例在本进程手里，它的轨迹才拷得到',
        received='本进程没有它——它不是本进程 spawn 的，或已被销毁',
        next_action=('在持有它的进程上分叉；要冷启动一个不继承上下文的 Agent 就不要传 fork_from'),
    )


def _cannot_fork_a_block(
    fork_from: str, position: int, role: str, exc: Exception
) -> ContractViolation:
    return ContractViolation(
        expected=f'分叉源 {fork_from!r} 的每一块轨迹都可深拷：分身改自己的轨迹不得污染源',
        received=f'第 {position} 块（role={role!r}）拷不动：{type(exc).__name__}: {exc}',
        next_action=(
            '把这一块的 extra / content 里活的对象（线程锁、连接、文件句柄）换成一段可深拷的'
            '描述再分叉——轨迹要持久化、要经压缩重写，活的对象在那两条路上同样过不去。'
            '本次分叉什么都没发生：分身既没有注册，也没有驱动任务'
        ),
    )


def _cannot_register_metadata(
    agent_id: str, metadata: dict[str, Any], exc: Exception
) -> ContractViolation:
    return ContractViolation(
        expected=f'agent {agent_id!r} 的 metadata 是纯数据，才能深拷进注册表',
        received=_why_copying_failed(  # pyright: ignore[reportPrivateUsage]
            metadata, exc, container='metadata'
        ),
        next_action=(
            '把 metadata 里活的对象（线程锁、连接、文件句柄）换成一段可深拷的描述再 spawn。'
            '本次 spawn 什么都没发生：条目没有注册，也没有驱动任务'
        ),
    )


def _cannot_wait_in_turn(agent_id: str, received: str) -> ContractViolation:
    return ContractViolation(
        expected=f'agent {agent_id!r} 的事件循环由本进程接管着，可以代表它等另一个 Agent',
        received=received,
        next_action=(
            '在轮等待由该 Agent 自己的编排工具在轮中发起，等的是另一个 Agent 的事件循环；'
            '要从 Runtime 外面等一个 Agent 结束就用 wait()。'
            '确认 agent_id 是 spawn 的返回值，并用 find() 读它当前的 status 与 owner'
        ),
    )


def _cannot_wait_on_itself(agent_id: str) -> ContractViolation:
    """判据是**两个 id 相等**，不是任务身份。

    `_refuse_self` 比对 `current_task()`，而一个声明了可并发的工具跑在 `gather` 建的子任务里、
    认不出自己（D-39）。这里两个 id 相等本身就无法满足，因此判据不依赖调用方跑在哪个任务上。
    """
    return ContractViolation(
        expected='在轮等待的目标是**另一个** Agent 的事件循环',
        received=f'agent_id 与 target_id 都是 {agent_id!r}',
        next_action=(
            '等自己的事件循环结束就是永久挂死：它停不下来，正是因为这一轮悬在这次等待上。'
            '要等一个子 Agent 就传 spawn 交回的那个 id；要让自己这一轮停下来用 abort'
        ),
    )


def _unknown_in_turn_target(agent_id: str, target_id: str, known: list[str]) -> ContractViolation:
    return ContractViolation(
        expected=f'在轮等待的目标 {target_id!r} 由本进程 spawn，且结束后仍留在本进程里',
        received=f'本进程没有它的运行记录（已知: {known}）：它不是本进程 spawn 的，或已被销毁',
        next_action=(
            f'传 spawn 交回的那个 agent_id——agent {agent_id!r} 这一轮悬在这次等待上，'
            '目标不存在时没有任何东西能把它叫回来。'
            '以 ExitMode.EXIT 结束的 Agent 连 Context 一起被销毁、等不到，改用 ZOMBIE spawn 它；'
            '目标由别的进程持有就在那个进程上等它'
        ),
    )


@dataclass(frozen=True)
class _Seed:
    """第一个周期的输入。

    `trust` 与 `text` 同生同死：只有存在一条 query 时才有「它是什么的衍生物」这个问题，于是
    「复活出来的循环没有 query」这件事在类型上就等于「没有 trust 可言」，不必传一个读不到的值。
    """

    text: str
    trust: TrustLevel


@dataclass(frozen=True)
class _InTurnWaitResult:
    """`Runtime._wait_in_turn` 的结局。

    **刻意不进公开面**：唯一的消费者是本包里的编排工具，而加一个公开名字不是破坏性变更、去掉一个
    是。第三方要它时再放出来。

    四态：`ANSWERED`（被等的那个事件循环结束了）、`ABORTED`、`PREEMPTED`、`TIMEOUT`。最后一态
    只在 Agent 的 `max_wait_seconds` Flag 为正时出现。

    **只有 `ANSWERED` 带 `context`，且 `ANSWERED` 必须带。** 被打断或超时的等待没有轨迹可交，
    硬凑一份会让调用方把「它还在跑」读成「它已经跑完」；而没有轨迹的 `ANSWERED` 在调用方那里
    表现为一次拿不到结果的成功。两者都是产出方的缺陷，在构造处拦下比在使用处现身便宜得多。
    """

    state: WaitState
    context: Context | None = None

    def __post_init__(self) -> None:
        answered = self.state is WaitState.ANSWERED
        if answered != (self.context is not None):
            raise ContractViolation(
                expected='context 与 state 一致：answered 带上被等到的那份轨迹，其余两种不带',
                received=(
                    f'收到 state={self.state.name}，context='
                    f'{"一份轨迹" if self.context is not None else "None"}'
                ),
                next_action=(
                    'state 是 answered 就把那个 Agent 的 Context 交出来；被中止或被顶掉时把'
                    ' context 留空——调用方按 state 分支，一份对不上的轨迹会被当成结果读下去'
                ),
            )


class _WaitToken:
    """一次等待的中断句柄。抢占与中止都经它送达。

    **先到者胜**：一次等待只返回一个结果，而两种结局的后续动作不同（被顶掉的可以重来，被中止的
    不该重来），所以第一个说法就是最终说法。

    结局与唤醒是同一个 Future，因此「被唤醒了却说不出结局」这个状态表示不出来。
    """

    def __init__(self) -> None:
        self.raised: asyncio.Future[WaitState] = asyncio.get_running_loop().create_future()

    def interrupt(self, state: WaitState) -> None:
        if not self.raised.done():
            self.raised.set_result(state)


@dataclass
class _Slot:
    """本进程为一个 Agent 额外持有的东西。

    实例与任务在 `LocalAgentTable`，可序列化状态在 `AgentRegistry`；这里放的是两边都不该管的
    第三样：这轮工作结束后该去哪（`exit_mode`）、控制块与门、造出它的那个工厂，以及**本进程的
    驱动任务到底接管过它没有**（`owned`——接管失败与从未 spawn 是两回事，`wait` 要分开答）。

    `factory` 留着是为了分叉：`Fork` 要照着源 Agent 再造一个**同类型**的，而「同类型」的实际
    含义就是同一个工厂——同一份 System Prompt、同一套工具与次序（spec §5.2 纪律 4：同类型分叉
    继承缓存亲和）。`installer` 是那个工厂当初按谁的身份跑的：分叉时 `Fork` 在源 Agent 的驱动
    任务里调 spawn，那里没有身份，照它再跑才不会把 Module 的工厂当成宿主的。
    """

    exit_mode: ExitMode
    control: ControlBlock
    gate: BrokerTurnGate
    factory: AgentFactory
    flags: Mapping[str, FlagValue]
    installer: Installer | None
    owned: bool = False
    # 此刻代表这个 Agent 在等答复的那一个。一个 Agent 是一份注意力，因此这里是一个而不是一组；
    # 按 agent 分槽而不是放在 Runtime 上，一次抢占或中止才只落在被点名的那个 Agent 上。
    waiter: _WaitToken | None = None
    # 此刻代表这个 Agent 在等另一个 Agent 干完活的那一个（派活 / 分叉）。与 `waiter` **分开**：
    # `_resumed_status` 拿 `waiter` 当「这一轮悬在一条匹配的答复上」的凭据，派活期间复用它会让
    # 一次 resume 把注册表写成 WAITING，而那一轮其实在往前走。
    delegation: _WaitToken | None = None
    # 事件循环此刻停在邮箱上（>0）还是在往前走（0）。注册表答不出这件事：停在邮箱上的 PAUSED
    # 与停在挂起门上的 PAUSED 是同一个取值，而只有后者收得到唤醒边沿。读它与改它都是同步的，
    # 因此控制面在一段不含 await 的代码里读到的答案与驱动任务此刻的位置严格一致。
    parked_on_mailbox: int = 0
    # 这个 Agent 的事件循环在工具调用之外派的活、发的消息数到第几个了（M3 D10）。放在这里而不是驱动
    # 任务里：zombie 被唤醒时换一个驱动任务，序号要接着数，否则两次唤醒的第一个撞成同一个键。
    issued: dict[str, int] = field(default_factory=dict[str, int])


@dataclass
class _Teardown:
    """一个条目上在飞的那些 `_destroy` 共享的一份记录。

    `holders` 是此刻有几个 `_destroy` 在拆它——最后一个离开的把这条记录摘掉，因此还在飞的那些
    看到的永远是同一份。`reported` 是**这次消失的收据**：拆除的每一步都是幂等的，多条级联可以
    各自把同一个节点摘一遍，收据只保证同一次消失不被报两遍 `AGENT_DESPAWN`。它**不是**「谁负责
    拆」的约定——没有任何调用方因为它而少拆一步，因此没有人的后置条件依赖别人兑现。
    """

    holders: int = 0
    reported: bool = False


def _parked_status(slot: _Slot) -> AgentStatus:
    """事件循环停到邮箱上时，注册表该说的那个状态。

    门落着就说 PAUSED：一次穿透挂起门的 `abort` 会把状态从 ABORTING 带回这一点，而此刻写 IDLE
    等于宣布「它能处理消息」——门还落着，它一条都不会处理，而 `send` 照答 ok=True。
    """
    return AgentStatus.PAUSED if slot.control.paused else AgentStatus.IDLE


def _resumed_status(slot: _Slot) -> AgentStatus:
    """解除挂起时该回到的状态。

    在飞的等待优先于 `in_run`：挂起期间发起的等待此刻仍挂着，写 RUNNING 等于宣布这一轮在往前走，
    而它悬在一次等待上、只有一条匹配的答复救得回来。

    这一问是同步的，但它与 `_resume_one` 那次 CAS 之间隔着一次 await：恰好在那段窗口里结束的等待会
    让注册表停在 `WAITING` 而槽里已经没有 waiter。**刻意留着这个短暂的谎**——下一个轮边界的
    `_oscillate` 会把它改回去（它的 `allowed` 含 `WAITING`），而「CAS 之后复检 waiter、发现没了就
    改成 RUNNING」这个修法自己会制造更糟的缺陷：那次复检与改写之间同样有窗口，一次刚装上的新等待
    会被它抹成 RUNNING——正是 `is token` 守卫存在的那个后果。
    """
    if slot.waiter is not None:
        return AgentStatus.WAITING
    return AgentStatus.RUNNING if slot.control.in_run else AgentStatus.IDLE


def _wait_ceiling(flags: Mapping[str, FlagValue]) -> float | None:
    seconds = flags.get('max_wait_seconds', 0)
    return float(seconds) if type(seconds) is int and seconds > 0 else None


def _cannot_wait_for(agent_id: str, received: str) -> ContractViolation:
    return ContractViolation(
        expected=f'agent {agent_id!r} 的事件循环由本进程接管着，可以代表它等一条答复',
        received=received,
        next_action=(
            'wait_for 由该 Agent 自己的工具在轮中发起，等的是一条匹配的答复；'
            '要从外面等它这次工作结束用 wait()。'
            '确认 agent_id 是 spawn 的返回值，并用 find() 读它当前的 status 与 owner；'
            '它由别的进程持有就在那个进程上等它'
        ),
    )


def _not_running_to_wait(agent_id: str, info: AgentInfo | None) -> ContractViolation:
    """拒绝只留给「本进程真的答不出这个问题」：手上没有一轮活可以挂起。

    `paused` 与 `aborting` 都**不**走这条路——那两个状态下这个 Agent 手上仍有一轮，而一次拒绝换来的
    是 `ContractViolation` 从工具里原样上抛、把整个 run 打成 `error`。
    """
    return ContractViolation(
        expected=f'agent {agent_id!r} 手上有一轮活可以挂起（running 或已经悬在另一次等待上）',
        received=(
            f'它当前是 {info.status.value}' if info is not None else '注册表里已经没有这个条目'
        ),
        next_action=(
            'wait_for 由该 Agent 自己的工具在轮中发起；要从外面等它这次工作结束用 wait()。'
            '它停在邮箱上（idle）时先 send 一条消息把它叫起来，再由它的工具发起等待；'
            'zombie 的先 ensure_active 复活它；条目已经不在就重新 spawn 一个'
        ),
    )


def _parent_being_destroyed(parent_id: str) -> ContractViolation:
    return ContractViolation(
        expected=f'parent_id {parent_id!r} 不在被拆的过程中',
        received=(
            '它正在被 despawn（或以 ExitMode.EXIT 收尾），它的整棵子树都要消失：'
            '此刻挂上去的子代会带着一个指向已注销条目的 parent_id 留在注册表里，'
            'children_of 从此永远漏掉它，而它自己看起来还有个父'
        ),
        next_action=(
            '等这次拆除结束——用 find() 确认那个父已经不在注册表里——再决定把这个 Agent 挂到哪个'
            '父下面；它不需要父就把 parent_id 置为 None'
        ),
    )


def _entry_vanished_during_spawn(agent_id: str, parent_id: str | None) -> ContractViolation:
    return ContractViolation(
        expected=f'agent {agent_id!r} 刚注册进去的条目在 spawn 返回之前还在注册表里',
        received=(
            f'它在 spawn 之内就被注销了（parent_id: {parent_id!r}）：一条并发的级联在 register 与'
            ' ensure_queue 之间发现了它并把它拆掉，而级联在这段窗口里**跑完**时钉住早已撤掉——'
            '只复检「那个父此刻在不在被拆」看不见这一支。交出这个 agent_id 就是交出一个注册表里'
            '不存在的 Agent，并留下一份属于不可寻址 Agent 的 Context'
        ),
        next_action=(
            '不要拿这个 agent_id 去 send / wait，它不可寻址。'
            '用 find() 确认那个父还在不在，再决定把这个 Agent 挂到哪个父下面；'
            '它不需要父就把 parent_id 置为 None'
        ),
    )


def _already_shut_down() -> ContractViolation:
    return ContractViolation(
        expected='Runtime 尚未 shutdown，spawn 出来的 Agent 事件仍有订阅者能收到',
        received=(
            'shutdown() 已经跑过：事件总线已关闭，此后 emit 一律静默丢弃，'
            '这个 Agent 会照常干完活却完全不可观测'
        ),
        next_action='要再跑 Agent 就新建一个 Runtime；shutdown 是终态，同一个 Runtime 不重开',
    )


class _TighteningBroker:
    """`runtime.broker`：被收紧的 Module 的代码（它的工具、hook 与它们起的任务）经它直接投递的
    消息，先把 `trust` 收成 UNTRUSTED 再交给真正的 broker（M5 D10，D-63）。`send` 已经按调用方的
    身份收紧，而 broker 是存储层的公开协议，拿得到 runtime 的代码就能绕过 `send`。标记是消息自己的
    字段，远程 broker 序列化之后照样在。只转交协议里的方法、不设 `__getattr__`：第三方 broker 多出来
    的投递入口（`publish_many`、`requeue`）要是被原样透出，收紧就从那条路漏掉了（评审 F11）。"""

    def __init__(self, inner: MessageBroker, untrusted: Callable[[], bool]) -> None:
        self._inner = inner
        self._untrusted = untrusted

    async def ensure_queue(self, agent_id: str) -> None:
        await self._inner.ensure_queue(agent_id)

    async def publish(self, agent_id: str, message: Message) -> DeliveryResult:
        if self._untrusted() and message.trust is not TrustLevel.UNTRUSTED:
            message = replace(message, trust=TrustLevel.UNTRUSTED)
        return await self._inner.publish(agent_id, message)

    async def consume(self, agent_id: str, *, max_n: int = 0) -> list[Message]:
        return await self._inner.consume(agent_id, max_n=max_n)

    async def wait_for_messages(self, agent_id: str, timeout: float | None) -> bool:  # noqa: ASYNC109
        return await self._inner.wait_for_messages(agent_id, timeout)

    async def take_by_correlation(
        self,
        agent_id: str,
        correlation_id: str,
        timeout: float | None,  # noqa: ASYNC109
    ) -> Message | None:
        return await self._inner.take_by_correlation(agent_id, correlation_id, timeout)

    async def remove_queue(self, agent_id: str) -> None:
        await self._inner.remove_queue(agent_id)

    def on_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.on_missing_queue(cb)

    def off_missing_queue(self, cb: MissingQueueCallback) -> None:
        self._inner.off_missing_queue(cb)


def _checked_store(value: object, protocol: type[_StoreT], field: str, constructed: str) -> _StoreT:
    """registry / broker 必须是 Protocol 实例。类对象会通过鸭子检查（方法挂在类上），漏括号
    `Runtime(registry=InMemoryAgentRegistry)` 会把构造推迟到第一次 spawn 的裸 TypeError。
    """
    if isinstance(value, type):
        raise ConfigError(
            expected=f'{field} 是 {protocol.__name__} **实例**',
            received=f'收到类对象 {value.__name__} 本身',
            next_action=(
                f'加上括号：{constructed}。'
                '类对象上的方法都取得到，因此鸭子检查放不倒它，'
                f'而第一次真正调用会报出一条指着 {field} 自己签名的 TypeError'
            ),
        )
    if not isinstance(value, protocol):
        raise ConfigError(
            expected=f'{field} 实现 {protocol.__name__}',
            received=f'收到 {type(value).__name__}，没有 {protocol.__name__} 要求的方法',
            next_action=f'传入一个 {protocol.__name__} 实例，或省略 {field} 用默认内存实现',
        )
    return value


def _alias_for(slot: AgentSlot, key: str | None) -> str | None:
    """稳定别名。on-demand 没有别名：每次 occupy 都是新实例，resolve 永远是 None。"""
    if slot.cardinality == 'on-demand':
        if key is not None:
            raise ConfigError(
                expected=f'槽 {slot.name!r} 是 on-demand，不接受 key',
                received=f'key={key!r}',
                next_action='去掉 key。on-demand 每次 occupy 新建，不按键复用',
            )
        return None
    if slot.cardinality == 'singleton':
        if key is not None:
            raise ConfigError(
                expected=f'槽 {slot.name!r} 是 singleton，不接受 key',
                received=f'key={key!r}',
                next_action='去掉 key。singleton 的别名就是槽名',
            )
        return slot.name
    if not isinstance(key, str) or not key.strip():
        raise ConfigError(
            expected=f'槽 {slot.name!r} 是 keyed，key 是非空白字符串',
            received=f'key={key!r}',
            next_action='传入这次实例的键，例如 resolve(name, key="a")',
        )
    return f'{slot.name}:{key}'


def _tightened_max_turns(current: int | None, requested: int | None, role: str) -> int | None:
    """槽预算只许把实例 max_turns 收紧。放宽（含把有上限改成 null）当场失败。"""
    if requested is None:
        if current is None:
            return None
        raise ConfigError(
            expected=f'槽预算只收紧角色 {role!r} 的 max_turns={current}',
            received='turns=null，等于把有上限的角色改成不限轮次',
            next_action='传一个不超过角色 max_turns 的非负整数，或不写 budget',
        )
    if current is not None and requested > current:
        raise ConfigError(
            expected=f'槽预算只收紧，不放宽（角色 {role!r} 的 max_turns={current}）',
            received=f'turns={requested}',
            next_action='传一个不超过角色 max_turns 的整数，或另定义一个预算更大的角色',
        )
    return requested


@dataclass(frozen=True)
class _TopologyBinding:
    """装配时钉死的槽。artifact_kinds 是当时清单上的快照，之后改目录不影响已装配的实例。"""

    slot: AgentSlot
    artifact_kinds: tuple[str, ...]


class Runtime:
    """Agent 的生命周期驱动与控制面。

    `plugins` 是扩展装配的第二层（spec §5.3）：它们挂在本 Runtime 的 hook 中心上，对每个 Agent
    都生效，并且**在那个 Agent 自己的局部钩子之后**执行。

    `orchestration=True` 给每个 spawn 出来的 Agent 装上 spec §5.6 的五个编排工具。**默认关**：
    五个工具的 `description` 逐轮进请求前缀，每一轮都付这份 token，而多数 Agent 不派活。
    """

    def __init__(
        self,
        *,
        registry: object = None,
        broker: object = None,
        plugins: Sequence[Plugin] | None = None,
        agents: Mapping[str, AgentFactory] | None = None,
        orchestration: bool = False,
        modules: Sequence[RuntimeModule] | None = None,
        flags: Mapping[str, FlagValue] | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
        ids: IdSource | None = None,
        tracer: Tracer | None = None,
        untrusted_output_modules: Sequence[str] = (),
        workspace: WorkspaceRouter | None = None,
        sessions: SessionStore | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        # 校验在建任何东西之前：一张错的角色表换来的应当是一次什么都没发生的拒绝。
        validate_agent_factories(agents or {})
        validate_confirmation_handler(confirmation_handler)
        if sessions is not None and redactor is None:
            raise ConfigError(
                expected='给了 sessions= 就要一并给 redactor=（会话落盘前要脱敏，spec §12）',
                received='sessions= 非空而 redactor=None',
                next_action=(
                    '传与 TraceRecorder 相同的那个 Redactor（如 '
                    'RuleRedactor(secrets=..., rules=...)）：内核不读环境，默认脱敏器认不出宿主的'
                    '密钥值，忘了传等于把密钥原样写进会话文件'
                ),
            )
        if workspace is not None and not isinstance(cast(object, workspace), WorkspaceRouter):
            raise ConfigError(
                expected='workspace 是 WorkspaceRouter 或省略',
                received=f'收到 {type(workspace).__name__}',
                next_action='用 WorkspaceRouter(LocalWorkspace(...)) 包一层再交进来',
            )
        self._workspace = workspace
        self._sessions = sessions
        # 落盘前过脱敏（spec §12）：给了 sessions 就必须给 redactor（上面已校验）。
        # 这里的 RuleRedactor() 只在没配 sessions 时占位，不会被用到（那条路 _sessions 为空）。
        self._redactor: Redactor = redactor if redactor is not None else RuleRedactor()
        self._ids: IdSource = RandomIds() if ids is None else _checked_ids(ids)
        checked_tracer = None if tracer is None else _checked_tracer(tracer)
        # 既不在工具调用里、也没有属主的 spawn 与 send 是宿主自己的代码，按宿主的第几次取键；经拓扑
        # 槽的按槽、键与第几个（M3 D10）。宿主自己并发地 spawn 时这个序号随到达的先后。
        self._host_spawns = 0
        self._host_messages = 0
        self._slot_spawns: dict[tuple[str, str], int] = {}
        # 正在等的请求（等待方、关联 id）→ 登记过的稳定键，后登记的在后：对它的答复用最后一个取消息
        # id，而不是按到达的先后。带上等待方：关联 id 只在一个 Agent 的等待里唯一，两个 Agent 可以等
        # 同一个。一个栈而不是一格：同一个 Agent 的下一次等待在上一次还没收尾时就登记了，它被拒时
        # 要露出的是上一次的登记，抢占成功时上一次收尾摘掉的是它自己那一个。
        self._request_keys: dict[tuple[str, str], list[StableKey]] = {}
        # 插桩（M3 D2）：交给这里管的每个 Agent 与执行器。不在工具调用、也没有属主时发起的等待按
        # 宿主的第几次取 span 的结构键。
        self._tracing = _Tracing(checked_tracer)
        if self._workspace is not None:
            self._workspace._attach_tracing(self._tracing)  # pyright: ignore[reportPrivateUsage]
        self._host_waits = 0
        module_assembly = _RuntimeModuleAssembly(modules, flags)
        # 第三方 Module 的输出一律当作不可信（spec §12.2 对第三方 Skill 的同一条处理）：它们的工具
        # 交回的结果、它们的 hook 写下的跳过与改写，执行器都收紧成 UNTRUSTED。
        self._untrusted_output = _untrusted_output_modules(
            untrusted_output_modules, module_assembly.names
        )
        if self._workspace is not None:
            self._workspace._attach_write_floor(  # pyright: ignore[reportPrivateUsage]
                lambda: self._from_listed_module(TrustLevel.TRUSTED)
            )
        self.registry: AgentRegistry = (
            InMemoryAgentRegistry()
            if registry is None
            else _checked_store(registry, AgentRegistry, 'registry', 'InMemoryAgentRegistry()')
        )
        self.broker: MessageBroker = _TighteningBroker(
            InMemoryMessageBroker()
            if broker is None
            else _checked_store(broker, MessageBroker, 'broker', 'InMemoryMessageBroker()'),
            lambda: self._from_listed_module(TrustLevel.TRUSTED) is TrustLevel.UNTRUSTED,
        )
        self.events: EventBus = EventBus()
        self.hooks: HooksCenter = HooksCenter()
        self.hooks._serve_runtime()  # pyright: ignore[reportPrivateUsage]
        self.hooks._tighten_modules(self._untrusted_output)  # pyright: ignore[reportPrivateUsage]
        # 扩展切点被触发时记在当前 span 上；在本 Runtime 任何 span 之外触发的不记（没有可挂的
        # 地方）。
        self.hooks._observe_extensions(  # pyright: ignore[reportPrivateUsage]
            lambda name: self._tracing.event('junction.triggered', {'junction.name': name})
        )
        # Module 清单里的切点先于一切第三方代码（Runtime 插件的 register 是第一段）声明：名字在
        # 那之前被占住，别人就抢不走。
        self.hooks._reserve_owners(frozenset(module_assembly.names))  # pyright: ignore[reportPrivateUsage]
        module_assembly.declare_junctions(self.hooks)
        # 装配在挂 on_missing_queue 之前：一次被拒的装配不该在共享 broker 上留下一条指向构造失败
        # 的 Runtime 的回调。
        self._plugin_names: tuple[str, ...] = install_plugins(
            self.hooks, () if plugins is None else plugins
        )
        # 快照而不是共用宿主那一份：`agent_names` 是调用方拿去排障与拼错误消息的答案，共用则
        # 宿主此后每一次 append 都改掉了早先每个读者看到的那个答案。与 `AgentOptions.tools` 相反
        # ——那里共用是为了热插拔，而角色表的载入与同名覆盖归计划 3，在那一层收口。
        self._agents: dict[str, AgentFactory] = dict(agents or {})
        # 登记的工厂按登记它的那一方的身份跑，不按此刻调 spawn 的那一方（身份跟着代码走）。构造
        # Runtime 的就是宿主；`assemble` 谁都调得到，按调用方记。
        self._agent_installers: dict[str, Installer | None] = dict.fromkeys(self._agents)
        self._orchestration = orchestration
        self._confirmation_handler = confirmation_handler
        self._table: LocalAgentTable[BaseAgent] = LocalAgentTable()
        self._slots: dict[str, _Slot] = {}
        # 驱动任务 → 它那次事件循环停下时的轨迹（`_finish` 一开头取，早于进 zombie）。在轮等待读
        # 这一份而不是活的 Context：被等的那一方进了 zombie 就叫得醒，复活的周期可能排在等待者
        # 醒来之前，活的那份那时已经是下一个周期的了。按任务存，复活之后的周期也跑完了，等前一个
        # 的那一方读到的仍是前一个；弱引用，没人再握着那个任务时条目跟着消失。
        self._ended: weakref.WeakKeyDictionary[asyncio.Task[Any], Context] = (
            weakref.WeakKeyDictionary()
        )
        # 收尾在事件循环之外做，因此它自己是一个任务；不在实例表里，`shutdown` 的取消名单看不见
        # 它们，只能另等一次。
        self._reapers: set[asyncio.Task[None]] = set()
        # 此刻有收尾在飞的 Agent。一个 Agent 同时只有一次收尾：两次并发的收尾会各自走一遍级联，
        # 而它们在拆同一个节点时都会先看到「它还在」，于是同一个 Agent 被报两遍 despawn。
        self._reaping: set[str] = set()
        # 「正在被拆的那些条目」→ **有几条在飞的级联把它钉住了**。计数而不是集合：一个节点可以
        # 同时落在多条级联的钉住范围里，而释放必须只撤掉本次调用自己放上去的那一份（理由见模块
        # 文档）。成员判定仍是 `agent_id in self._doomed`。
        self._doomed: dict[str, int] = {}
        # 此刻正在被 `_destroy` 拆的条目 → 那些拆除共享的记录。它只承载「这次消失报过没有」这张
        # 收据：痕迹检查自带一个 await，两条重叠的拆除都会先看到「它还在」，于是各自摘一遍——
        # 摘是幂等的，报不是。**不用它让谁等谁**（理由见模块文档）。
        self._destroying: dict[str, _Teardown] = {}
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_waits_for: dict[Runtime, int] = {}
        # 拓扑别名与 `_slots` 不是一张表。`_slots` 是每个已 spawn Agent 的生命周期；别名是
        # 「槽名 → 当时的 agent_id」快照。复用 `_slots` 会让一次 occupy 盖掉驱动任务。
        self._assembled = False
        self._topology_bindings: dict[str, _TopologyBinding] = {}
        self._topology_aliases: dict[str, str] = {}
        self._topology_alias_owners: dict[str, str] = {}
        self._topology_tools: dict[str, Tool] = {}
        self._module_assembly = module_assembly
        self._module_assembly.install(self)
        # 「队列不存在」是懒启动与 ZOMBIE 复活的唯一触发点（spec §4.4）。接在这里，而不是让每个
        # 调用方自己先判断再激活——那样「复活」就有了 N 份实现。
        self.broker.on_missing_queue(self._on_missing_queue)

    @property
    def workspace(self) -> WorkspaceRouter | None:
        """构造时交进来的工作区路由；没给时是 None，那时 `spawn` 只能 `isolation='shared'`。"""
        return self._workspace

    async def merge_isolated(self, agent_id: str) -> MergeReport:
        """把一个隔离起来的 Agent 的副本逐文件合并回它拷出来的那个工作区并删掉副本（spec §6.4）。
        只在它这次工作已经结束（IDLE / ZOMBIE）时合并：暂停、等待、收尾中的 Agent 还在一个周期
        里，合并之后它余下的写会直接落进共享工作区。合并期间它被叫醒也写不进副本（路由拒绝）。"""
        router = self._isolating_router('merge_isolated')
        info = await self.registry.get(agent_id)
        if info is not None and info.status not in (AgentStatus.IDLE, AgentStatus.ZOMBIE):
            raise ConfigError(
                expected=f'合并时 agent {agent_id} 这次工作已经结束（IDLE / ZOMBIE）',
                received=f'它此刻是 {info.status.value}',
                next_action='用 wait_for 等它这次工作结束再合并；暂停它不算结束',
            )
        return await router.merge_isolated(agent_id)

    async def discard_isolated(self, agent_id: str) -> None:
        """丢掉一个 Agent 的隔离副本、不合并；此后它的调用落在共享工作区上。"""
        await self._isolating_router('discard_isolated').discard_isolated(agent_id)

    def _isolating_router(self, operation: str) -> WorkspaceRouter:
        if self._workspace is None:
            raise ConfigError(
                expected=f'{operation} 用在构造时交了 workspace 的 Runtime 上',
                received='这个 Runtime 没有工作区路由',
                next_action='Runtime(workspace=WorkspaceRouter(..., isolator=LocalIsolator(...)))',
            )
        return self._workspace

    @property
    def plugins(self) -> tuple[str, ...]:
        """已装插件的名字，按装配顺序。

        交回元组而不是列表：这是一份快照，而调用方拿它去排障（「装了哪些、按什么顺序」）。交出
        内部列表的话，第一个读者一次 `append` 就改了后来每个读者看到的答案。
        """
        return self._plugin_names

    @property
    def modules(self) -> tuple[str, ...]:
        """已装 Runtime Module 的名字，按装配顺序。"""
        return self._module_assembly.names

    @property
    def flags(self) -> Mapping[str, FlagValue]:
        """Runtime 级 Flag 的只读解析结果。"""
        return self._module_assembly.flags

    @property
    def flag_declarations(self) -> tuple[FlagDeclaration, ...]:
        """已装 Module 的 Flag 声明，按 Module 与声明顺序。"""
        return self._module_assembly.declarations

    @property
    def agent_names(self) -> tuple[str, ...]:
        """已登记的角色名，按登记顺序。

        顺序计入公开面：`Delegate` 在「这个角色名不存在」那条消息里逐字列出它，模型照着挑。交回
        元组而不是 `keys()` 视图——视图随这张表变，而调用方拿它去拼一条要交给模型的消息。
        """
        return tuple(self._agents)

    def agent_factory(self, name: str) -> AgentFactory | None:
        """按角色名取工厂；没有就 None（问句只答不抛）。

        这是「派活给谁」的最小落点。`assemble` 用目录 freeze 的工厂覆盖同名项；本方法仍只按名字取，
        没有就 None。
        """
        return self._agents.get(name)

    def _factory_of(self, agent_id: str) -> AgentFactory | None:
        """造出这个 Agent 的那个工厂；本进程不持有它则 None。

        `Fork` 要照着源 Agent 再造一个**同类型**的：spec §5.2 纪律 4 说同类型分叉继承缓存亲和，
        而「同类型」在这里的实际含义就是同一个工厂——同一份 System Prompt、同一套工具与次序。

        **私有**：唯一的消费者是本包里的 `Fork`，理由同 `_InTurnWaitResult`。
        """
        slot = self._slots.get(agent_id)
        return slot.factory if slot is not None else None

    def _provenance_of(self, agent_id: str) -> TrustLevel:
        """这个 Agent 的轨迹能不能溯源到不可信内容（spec §12.2）。只读，绝不激活。

        本进程手里没有它的实例时答 `UNTRUSTED`：唯一的消费者是「派出去的那件活该不该带着不可信
        标记」，而答不出来就按最坏情况处理（spec §13）——少判会让宿主配置的升级处置静默失效。

        **私有**：唯一的消费者是本包里的 `Delegate`，理由同 `_InTurnWaitResult`。判据本身
        （`context.provenance_of`）是模块级函数、单一定义，宿主要问同一个问题就用它。
        """
        agent = self._table.get(agent_id)
        if agent is None:
            return TrustLevel.UNTRUSTED
        return provenance_of(agent.context)

    async def spawn(
        self,
        factory: AgentFactory,
        *,
        query: str | None = None,
        query_trust: TrustLevel = TrustLevel.TRUSTED,
        exit_mode: ExitMode = ExitMode.ZOMBIE,
        parent_id: str | None = None,
        fork_from: str | None = None,
        metadata: dict[str, Any] | None = None,
        flags: Mapping[str, FlagValue] | None = None,
        isolation: str = 'shared',
        session: bool = False,
    ) -> str:
        """建一个 Agent 并启动它的事件循环，返回它的 agent_id。

        `isolation` 是 `shared`（缺省，与父共用工作区）、`worktree` 或 `snapshot`：后两者让它在
        共享工作区的一份拷贝里干活（spec §6.4），需要构造时交了能隔离的 `workspace`。副本在它的
        第一个周期之前就位，它驱动的任务里另起的任务继承驱动身份、同样落在副本上；「你在副本里」
        这件事写在 query 的开头（不进 System Prompt）。合并由调用方决定（`merge_isolated`）；
        `ExitMode.EXIT` 的 Agent 结束即拆除，副本随之丢弃并记一条警告。

        `query=None` 就是「起来等消息」（spec §4.7 的 lazy boot）：事件循环直接进 IDLE，第一条
        消息才是它的第一个周期。

        `query_trust` 是那条 query 的信任级别（spec §12.2 第 2 条）。它由**写下这段文字的那一方**
        给出：派活的 `query` 是父 Agent 的模型写的，而模型读过什么决定了那段文字是什么的衍生物。

        `fork_from` 让新 Agent 继承那个 Agent 到此刻为止的轨迹（逐块深拷，`trust` 一并带着）。
        末尾未配对的那些调用会被截掉（见 `_forkable_prefix`），而**被截掉的那些块可能携带这份
        轨迹里唯一的不可信标记**，所以这里把源 Agent 的溯源结论并进 `query_trust`：截断是本方法
        自己的动作，补偿它的责任也在这里。`query=None`（spec §4.7 的 lazy boot）没有 query 可挂，
        改挂到继承来的最后一块上（见 `_carry_truncated_taint`）——**两条路都必须补**：那个事实在
        本方法返回之后就不存在于任何地方了，宿主无从得知，下游也无从补救。

        工厂收下本 Runtime 的 hooks 与事件总线：Agent 自己新建一份，宿主注册的切点与订阅就都落空了。

        `session=True` 把它标成会话根（`AgentInfo.session_id = agent_id`，在注册那一刻写下、早于驱动
        任务起跑，不与第一个周期抢）：配了 `sessions` 的 Runtime 会在周期收尾 / 关机时把它存盘。只给
        宿主开一次会话的根 Agent 用；子 Agent 不是会话（root-only，spec §15）。
        """
        if isolation not in ISOLATIONS:
            raise ConfigError(
                expected=f'isolation 是 {" / ".join(ISOLATIONS)} 之一',
                received=f'isolation={isolation!r}',
                next_action='改成上面三者之一；不要隔离就用 shared',
            )
        if isolation != 'shared' and (self._workspace is None or not self._workspace.can_isolate):
            raise ConfigError(
                expected=f'isolation={isolation!r} 用在交了能隔离的 workspace 的 Runtime 上',
                received='这个 Runtime 没有工作区路由，或它的路由没有隔离器',
                next_action=(
                    'Runtime(workspace=WorkspaceRouter(..., isolator=LocalIsolator(...)))；'
                    '或者用 isolation=shared'
                ),
            )
        query_trust = self._from_listed_module(query_trust)
        # 取走而不是只读：工厂与 hook 里再 spawn 的 Agent 不是这个槽的，也不该拿这个槽的身份。
        slot_spawn = _SLOT_SPAWN.get()
        _SLOT_SPAWN.set(None)
        # 键在两道取决于调度的拒绝之前占用（并发的关机、并发的拆除）：被拒的这一次也占掉它的序号，
        # 后面的 spawn 才在两次运行里拿到同一个键（M3 D10）。
        key = self._agent_key(slot_spawn)
        if self._closed:
            raise _already_shut_down()
        if parent_id is not None and parent_id in self._doomed:
            raise _parent_being_destroyed(parent_id)
        # 先取 id 再调工厂：工厂里就有东西绑定 `agent.agent_id`。工厂返回一个不是在这次调用里构造
        # 的 Agent 时，它留着自己的 id。
        assigned = _fresh_id(self._ids.agent_id, key, 'agent_id')
        # cast 到 object 是因为要挡的正是违反 `AgentFactory` 标注的运行时取值；不擦掉静态类型，
        # 下面的 isinstance 就成了 pyright 眼里的死代码。
        module_hooks = HooksCenter(outer=self.hooks)
        module_hooks._serve_one_agent()  # pyright: ignore[reportPrivateUsage]
        runs_as = self._factory_installer(factory, slot_spawn, fork_from)
        token = assigning_agent_id(assigned)
        try:
            with acting_as(runs_as):
                produced = cast('object', factory(module_hooks, self.events))
        finally:
            releasing_agent_id(token)
        if not isinstance(produced, BaseAgent):
            raise _bad_factory_return(factory, produced)
        agent = produced
        # 工厂交回的 Agent 可能建在别的中心上（在别处建好的实例）：Module 插件照样装进它的 Module
        # 层，所以那一层同样标成「服务单个 Agent、属于 Runtime」——挂不上只由 Runtime 触发的切点，
        # 公开入口也触发不了内核切点。建在 Runtime 中心本身上的不能这样标：那会让 Runtime 级也
        # 挂不上那几个切点，Module 插件也会落到每个 Agent 身上。
        if agent._module_hooks is self.hooks:  # pyright: ignore[reportPrivateUsage]
            raise _agent_on_the_runtime_center(factory)
        agent._module_hooks._serve_one_agent()  # pyright: ignore[reportPrivateUsage]
        agent._module_hooks._serve_runtime()  # pyright: ignore[reportPrivateUsage]
        agent_id = agent.agent_id
        # 按类型比而不是 isinstance：记录用的源最自然的写法就是继承 RandomIds，它要这条警告。
        if agent_id != assigned and type(self._ids) is not RandomIds:
            logger.warning(
                '期望: 工厂交回的 Agent 用 spawn 交给它的 id %s（键 %r）。'
                ' 实际: 它交回的 Agent 的 id 是 %s——它不是在这次工厂调用里构造的，'
                '或工厂先构造了别的 Agent，那一个拿走了这个 id。'
                ' 下一步: 让工厂在这次调用里先构造并交回这个 Agent；'
                '否则回放时它拿不到记录里的 id。',
                assigned,
                key,
                agent_id,
            )
        assembly_snapshot = agent._assembly_snapshot()  # pyright: ignore[reportPrivateUsage]
        inherited_flags: Mapping[str, FlagValue] | None = None
        source: BaseAgent | None = None
        source_trust = TrustLevel.TRUSTED
        forked_blocks: list[ContextBlock] = []
        if fork_from is not None:
            source = self._table.get(fork_from)
            if source is None:
                raise _cannot_fork_from(fork_from)
            source_slot = self._slots.get(fork_from)
            if source_slot is None:
                raise _cannot_fork_from(fork_from)
            inherited_flags = source_slot.flags
            # 溯源结论在截断**之前**取：被截掉的块可能是这份轨迹里唯一不可信的那一块。
            source_trust = provenance_of(source.context)
            query_trust = strictest_trust(query_trust, source_trust)
            for position, block in enumerate(_forkable_prefix(source.context.current_traj)):
                try:
                    forked_blocks.append(copy.deepcopy(block))
                except Exception as exc:
                    raise _cannot_fork_a_block(fork_from, position, block.role, exc) from exc
        elif parent_id is not None:
            parent = await self.registry.get(parent_id)
            if parent is not None:
                inherited_flags = parent.flags
        agent_flags = self._module_assembly.agent_flags(flags, inherited=inherited_flags)
        try:
            owned_metadata = copy.deepcopy(metadata or {})
        except Exception as exc:
            raise _cannot_register_metadata(agent_id, metadata or {}, exc) from exc
        control = ControlBlock()
        gate = BrokerTurnGate(agent_id, self.broker, control)
        # Module 的工厂与 install_own_tools 拿得到 runtime，可能往 Runtime 级挂 hook：装配失败时一并
        # 还原。快照取在最后一个 await 之后：装配这段同步执行，还原不会盖掉别人同时挂上的 hook。
        runtime_hooks = self.hooks._assembly_snapshot()  # pyright: ignore[reportPrivateUsage]
        try:
            role_tools = frozenset(agent.options.tools)
            self._install_orchestration(agent, agent_id)
            module_tools, module_plugins = self._module_assembly.install_agent(
                self, agent, agent_flags
            )
            self._install_slot_tools(agent, role_tools, module_tools)
            installed_by_modules = {name: agent.options.tools[name] for name in module_tools}
            agent._configure_untrusted_output(  # pyright: ignore[reportPrivateUsage]
                frozenset(
                    name for name, owner in module_tools.items() if owner in self._untrusted_output
                ),
                self._untrusted_output,
                module_tools,
            )
            # 旗子只决定其它角色。Orchestrator 在旗子关掉时仍要能派活，所以每个角色都有一次
            # 自己装工具的机会；缺省实现什么都不做。
            before_own = dict(agent.options.tools)
            with acting_as(runs_as):
                agent.install_own_tools(self)
            for name, tool in agent.options.tools.items():
                if before_own.get(name) is not tool:
                    require_own_command_args(
                        tool, name, f'类式角色 {type(agent).__qualname__} 的 install_own_tools'
                    )
            replaced = sorted(
                name
                for name, tool in installed_by_modules.items()
                if agent.options.tools.get(name) is not tool
            )
            if replaced:
                raise _module_tool_replaced(type(agent), replaced, module_tools)
            agent._configure_runtime_flags(  # pyright: ignore[reportPrivateUsage]
                agent_flags
            )
            agent._configure_confirmation(  # pyright: ignore[reportPrivateUsage]
                None if self._confirmation_handler is None else self._confirm
            )
            for block in forked_blocks:
                agent.context.append(block)
            if fork_from is not None and query is None:
                self._carry_truncated_taint(agent, fork_from, source_trust)
            agent._attach_gate(gate)  # pyright: ignore[reportPrivateUsage]
            agent._attach_tracing(self._tracing)  # pyright: ignore[reportPrivateUsage]
        except BaseException:
            agent._assembly_restore(assembly_snapshot)  # pyright: ignore[reportPrivateUsage]
            self.hooks._assembly_restore(runtime_hooks)  # pyright: ignore[reportPrivateUsage]
            raise
        # 与快照同一段同步代码里取：此后的 await 期间别的 spawn 也会往 Runtime 级挂 hook，失败时只撤
        # 这一次装上的。
        runtime_added = self.hooks._added_since(runtime_hooks)  # pyright: ignore[reportPrivateUsage]
        registered = False
        queue_attempted = False
        try:
            await self.registry.register(
                AgentInfo(
                    agent_id=agent_id,
                    name=agent.options.name,
                    agent_type=type(agent).__name__,
                    parent_id=parent_id,
                    session_id=agent_id if session else None,
                    metadata=owned_metadata,
                    flags=dict(agent_flags),
                )
            )
            registered = True
            queue_attempted = True
            await self.broker.ensure_queue(agent_id)
        except BaseException:
            await self._rollback_unstarted_spawn(
                agent_id,
                agent,
                assembly_snapshot,
                runtime_added,
                registered=registered,
                queue_attempted=queue_attempted,
            )
            raise
        # `_closed` 与 `_doomed` 在方法开头各读过一次，此后隔着 register / ensure_queue 两个
        # Protocol await。任何会挂起的 store 下，并发的 shutdown / despawn 会在这两个 await 期间
        # 插进来，于是：关机那一支建出一个永不被取消、永不被 await 的驱动，并把一个再没有订阅者的
        # Agent 留在注册表里；拆除那一支交出一个级联刚刚注销掉的 agent_id 却报成功，留下一份属于
        # 不可寻址 Agent 的 Context，并让同一个 agent_id 先 `AGENT_DESPAWN` 后 `AGENT_SPAWN`。
        #
        # 复检的是**后置条件**（我注册的那一条还在不在），不只是标志位：级联若在这段窗口里跑完，
        # 钉住早已撤掉，`_doomed` 那一问答「没在被拆」而它注销掉的正是这一条。两问合起来没有缝，
        # 因为能注销这个条目的级联必然先把它钉住、且钉住活到该级联自己结束：还在飞的被 `_doomed`
        # 按住，已经跑完的被这次 get 按住。顺序是**先带 await 的那一问、再读两个同步标志位**，
        # 于是最后一道守卫与 create_task 之间没有挂起点。
        refusal: ContractViolation | None = None
        if await self.registry.get(agent_id) is None:
            refusal = _entry_vanished_during_spawn(agent_id, parent_id)
        elif self._closed:
            refusal = _already_shut_down()
        elif parent_id is not None and parent_id in self._doomed:
            refusal = _parent_being_destroyed(parent_id)
        if refusal is not None:
            await self._rollback_unstarted_spawn(
                agent_id,
                agent,
                assembly_snapshot,
                runtime_added,
                registered=True,
                queue_attempted=True,
            )
            raise refusal
        if self._workspace is not None:
            self._workspace.adopt(agent_id, parent_id)
            router = self._workspace
            agent._executor._configure_file_trust(  # pyright: ignore[reportPrivateUsage]
                lambda path: router.script_trust(path)
            )
        if isolation != 'shared':
            assert self._workspace is not None
            # 同步建副本：最后一道守卫与 create_task 之间仍然没有挂起点。
            try:
                isolated = self._workspace.isolate(agent_id, isolation)
            except BaseException:
                await self._workspace.release(agent_id)
                await self._rollback_unstarted_spawn(
                    agent_id,
                    agent,
                    assembly_snapshot,
                    runtime_added,
                    registered=True,
                    queue_attempted=True,
                )
                raise
            if isolated is not None and query is not None:
                query = f'{isolated.fact()}\n\n{query}'
        # put 先于 create_task：put 被拒时还没有任务可泄漏。两者之间没有挂起点，任务因此不会在
        # set_task 之前就跑起来。
        self._table.put(agent_id, agent)
        self._slots[agent_id] = _Slot(
            exit_mode=exit_mode,
            control=control,
            gate=gate,
            factory=factory,
            flags=agent_flags,
            installer=runs_as,
        )
        seed = None if query is None else _Seed(text=query, trust=query_trust)
        task = asyncio.create_task(self._drive(agent_id, agent, seed), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, task)
        # 派生边在任务建好之后：put 与 create_task 之间一句别的都不放。终点是它在这里的第一个周期
        # ——工厂交回的可能是一个已经跑过几个周期的 Agent。
        self._tracing.spawned(agent_id, agent._cycle + 1)  # pyright: ignore[reportPrivateUsage]
        self._trace_modules(agent_id, module_tools, module_plugins)
        await self.hooks._dispatch(Junction.AGENT_SPAWN, agent_id=agent_id, parent_id=parent_id)  # pyright: ignore[reportPrivateUsage]
        return agent_id

    def _trace_modules(
        self, agent_id: str, tools: Mapping[str, str], plugins: Mapping[str, str]
    ) -> None:
        """每个已装的 Module 一个开即关的根 span（只声明 Flag 或服务的也有）：trace 自己就说得清
        这个 Agent 在哪些 Module 下跑、各自给了它什么。只在 spawn 提交之后记，回滚掉的 spawn 不留
        痕迹。"""
        for module in self._module_assembly.module_objects:
            attributes: dict[str, AttributeValue] = {
                'module.name': module.name,
                'module.version': module.version,
                'module.tools': [name for name, owner in tools.items() if owner == module.name],
                'module.plugins': [name for name, owner in plugins.items() if owner == module.name],
                'module.flags': [flag.key for flag in module.flags],
            }
            with self._tracing.span(
                SpanKind.MODULE,
                (agent_id, module.name),
                name=module.name,
                agent_id=agent_id,
                attributes=attributes,
                detached=True,
            ):
                pass

    async def _confirm(self, request: ConfirmationRequest) -> ConfirmationOutcome:
        handler = self._confirmation_handler
        if handler is None:
            raise ContractViolation(
                expected='Runtime 有 confirmation_handler 才会发起确认',
                received='没有 handler 却进入了确认 callback',
                next_action=(
                    '不要绕过 spawn 的确认绑定；需要确认时构造 Runtime(confirmation_handler=...)'
                ),
            )
        return await handler.confirm(self, request)

    def _agent_key(self, slot: _SlotSpawn | None) -> StableKey:
        """这次 spawn 的稳定键：当前出处（工具调用或属主）里的第几个，否则是宿主的第几次。

        经拓扑槽的在前面加上槽名与键。on-demand 槽的实例属于要它的那次调用，跟着调用方的出处取
        （不在任何出处里时按这个槽的第几个）；singleton 与 keyed 的按这个槽、这个键的第几个，**不
        占用调用方的计数**——它们由槽与键确定，谁先来寻址都是同一个。
        """
        if slot is not None:
            if slot.per_caller:
                origin = next_key('spawn')
                if origin is not None:
                    return ('slot', slot.name, slot.key, *origin)
            count = self._slot_spawns.get((slot.name, slot.key), 0)
            self._slot_spawns[(slot.name, slot.key)] = count + 1
            return ('slot', slot.name, slot.key, count)
        origin = next_key('spawn')
        if origin is not None:
            return origin
        self._host_spawns += 1
        return ('host', 'spawn', self._host_spawns - 1)

    def _message_key(self, receiver: str, correlation_id: object) -> StableKey:
        """这条消息的稳定键：答复收件人正在等的请求时用那个请求的键，否则是当前出处里的第几条，或
        宿主的第几条。"""
        # 只认字符串：关联 id 的类型由 broker 那一侧把关，这里一个不可哈希的值不该变成裸 TypeError。
        waiting = (
            self._request_keys.get((receiver, correlation_id))
            if isinstance(correlation_id, str)
            else None
        )
        if waiting:
            request = waiting.pop()
            if not waiting:
                del self._request_keys[(receiver, cast(str, correlation_id))]
            return (*request, 'reply')
        key = next_key('message')
        if key is not None:
            return key
        self._host_messages += 1
        return ('host', 'message', self._host_messages - 1)

    def _install_orchestration(self, agent: BaseAgent, agent_id: str) -> None:
        """把 spec §5.6 的五个编排工具挂进这个 Agent。

        **同名工具报错而不是覆盖**：静默覆盖会让这个 Agent 自己那一个永远调不到，而 run 照常以
        COMPLETED 返回——失败被当成成功上报。抛在 `register` 之前，被拒的 spawn 什么都没发生。

        装配经 `BaseAgent.install_runtime_tools`，**不就地改 `agent.options.tools`**：那张 dict 由
        宿主的工厂持有，而工厂可以在多次调用之间复用同一个，就地写会把它变成「属于第一个 Agent」。
        """
        if not self._orchestration:
            return
        installed = orchestration_tools(self, agent_id)
        clashes = sorted(set(installed) & set(agent.options.tools))
        if clashes:
            raise _orchestration_name_clash(agent.options.name, clashes)
        agent.install_runtime_tools(installed)

    def _install_slot_tools(
        self, agent: BaseAgent, role_tools: frozenset[str], module_tools: Mapping[str, str]
    ) -> None:
        """装槽工厂挂在 Agent 上的 extra_tools：排在编排工具与 Module 工具之后（M4 D2）。

        撞名报错而不是覆盖，理由同 `_install_orchestration`；错误点名对方是谁，因为三方都可能是
        先装上的那一个，而槽的作者只看得见自己写的那几个名字。
        """
        held = agent._held_slot_tools()  # pyright: ignore[reportPrivateUsage]
        if held is None:
            return
        slot, role, tools = held
        replaced = sorted(name for name in tools if name in role_tools)
        if replaced:
            raise ConfigError(
                expected=f'槽 {slot!r} 的 extra_tools 只并上角色还没有的工具',
                received=f'这些名字已经在角色 {role!r} 上：{replaced}。增强不得替换',
                next_action='需要不同行为就定义不同角色；槽不得替换基础工具',
            )
        clashes = sorted(name for name in tools if name in agent.options.tools)
        if clashes:
            owners = '、'.join(
                f'{name}（'
                + (f'Module {module_tools[name]!r} 的工具' if name in module_tools else '编排工具')
                + '）'
                for name in clashes
            )
            raise ConfigError(
                expected=f'槽 {slot!r} 的 extra_tools 只并上 Agent 还没有的工具',
                received=f'装槽工具之前这些名字已经装上：{owners}',
                next_action=(
                    '从槽的 extra_tools 里去掉它们：编排工具与 Module 工具每个 Agent 都已经有了；'
                    '要换一种行为就给 Module 的工具换名，或定义不同角色'
                ),
            )
        agent.install_runtime_tools(tools)

    def _carry_truncated_taint(
        self, agent: BaseAgent, fork_from: str, source_trust: TrustLevel
    ) -> None:
        """`query=None` 的分叉：把源 Agent 的溯源结论挂到继承来的最后一块上。

        `query_trust` 是另一条路上的落点，而 lazy boot 没有 query 可挂。补偿必须留在**丢弃发生的
        这一处**：「截断丢掉了一个不可信块」这个事实在 `spawn` 返回的那一刻就不存在于任何地方，
        宿主与下游都无从得知，因此 `send(trust=...)` 这类下游入口补不上它。

        判据仍是 `context.provenance_of` 那个上界（「轨迹里出现过 `UNTRUSTED` 的块」），所以收紧
        一块继承来的 `trust` 就够，不引入第二套启发式。改的是 `trust` 而不是追加一块说明：`trust`
        不进请求字节，分身的前缀因此与源逐字节相同（spec §5.2 纪律 4 的缓存亲和是分叉的全部意义），
        而一块内核自撰的说明既改前缀、又会被模型读到并据此行动。
        """
        if source_trust is not TrustLevel.UNTRUSTED:
            return
        blocks = agent.context.current_traj
        if blocks:
            # `current_traj` 只拷列表、不拷块，所以这一句落在轨迹里那一块上。
            blocks[-1].trust = TrustLevel.UNTRUSTED
            return
        logger.error(
            '期望: 从 agent %s 分叉出来的 %s 带着源的溯源结论 untrusted——'
            '截断丢掉的块可能携带那份轨迹里唯一的不可信标记。'
            ' 实际: 截断之后一块都没剩下，而这次分叉没有 query 可挂，标记无处安放：'
            '分身的轨迹于是干净得像什么都没读过，它此后的高风险调用会绕过 spec §12.2 第 3 条'
            '那道强制升级。'
            ' 下一步: 给这次分叉传一个 query（它会带上合并后的 query_trust），'
            '或者别让分身继承一份只剩未配对批次的轨迹——先让源 Agent 跑完那一批再分叉。',
            fork_from,
            agent.agent_id,
        )

    def _from_listed_module(self, trust: TrustLevel) -> TrustLevel:
        """被收紧的 Module 的代码（它的工具、hook、`on_start` 与它们起的任务）经 `send` /
        `send_to_slot` / `spawn(query=)` 写下的文字同样不可信：它们拿得到 runtime，而缺省的
        `TRUSTED` 会让收件人的高风险调用绕过 §12.2 的升级。宿主代码没有身份，照它给的算。"""
        installer = current_installer()
        if (
            installer is not None
            and installer.kind == 'module'
            and installer.name in self._untrusted_output
        ):
            return TrustLevel.UNTRUSTED
        return trust

    def _factory_installer(
        self, factory: AgentFactory, slot: _SlotSpawn | None, fork_from: str | None
    ) -> Installer | None:
        """工厂与 `install_own_tools` 按把这个工厂交给内核的那一方的身份跑：登记的角色按登记时的，
        经拓扑槽的按槽上那个角色的，分叉按源 Agent 当初的，调用方自己交给 spawn 的按调用方的。

        宿主登记的角色若继承起它的 Module 的身份，它自己挂的 hook 写下的文字会被收紧，它声明扩展
        切点会被当成 Module 越权；反过来，把 Module 交进来的工厂当成宿主的，就把它写下的文字放成了
        可信。内核分辨不出一个交进来的函数是谁写的，所以只认这几条登记过的来路。"""
        if slot is not None:
            return slot.installer
        if fork_from is not None:
            source = self._slots.get(fork_from)
            if source is not None and source.factory is factory:
                return source.installer
        for name, registered in self._agents.items():
            if registered is factory:
                return self._agent_installers[name]
        return current_installer()

    async def _rollback_unstarted_spawn(
        self,
        agent_id: str,
        agent: BaseAgent,
        assembly_snapshot: tuple[Any, ...],
        runtime_added: Any,
        *,
        registered: bool,
        queue_attempted: bool,
    ) -> None:
        """回滚尚未创建驱动任务的 spawn，不让清理失败盖掉原始异常。"""
        cleanups: list[tuple[str, Callable[[str], Awaitable[None]]]] = []
        if queue_attempted:
            cleanups.append(('remove_queue', self.broker.remove_queue))
        if registered:
            cleanups.append(('unregister', self.registry.unregister))
        for operation, cleanup in cleanups:
            try:
                await cleanup(agent_id)
            except BaseException:
                logger.exception(
                    '期望: spawn 回滚时 %s(%r) 成功或幂等返回。'
                    ' 实际: 清理抛出异常，原始 spawn 失败会继续向上传播。'
                    ' 下一步: 修复对应 Store 的幂等清理；随后确认邮箱、注册表和父子索引没有残留。',
                    operation,
                    agent_id,
                )
        agent._assembly_restore(assembly_snapshot)  # pyright: ignore[reportPrivateUsage]
        self.hooks._withdraw(runtime_added)  # pyright: ignore[reportPrivateUsage]

    async def _unbuild(self, agent_id: str) -> None:
        """把 `spawn` 已经建好的两样按逆序还回去，不留半个 Agent。

        两步都对「已经不在」是 no-op（Protocol 明文），因此一条并发的级联抢先摘掉其中之一时这里
        照样收干净——那正是 `_doomed` 这道复检要处理的局面。
        """
        await self.broker.remove_queue(agent_id)
        await self.registry.unregister(agent_id)

    async def _drive(self, agent_id: str, agent: BaseAgent, seed: _Seed | None) -> None:
        _DRIVING_AGENT.set(agent_id)
        no_installer()
        # `get` 而不是下标：任务是 create_task 排上去的，在它真正跑起来之前 slot 可能已经被摘掉。
        # 那时该静默退出，而不是让一个 KeyError 变成无人认领的任务异常。
        slot = self._slots.get(agent_id)
        if slot is None:
            return
        # 驱动任务建在发送方的上下文里（spawn、唤醒 zombie 的那次 send）：不换属主就继承了别人的
        # 「当前工具调用」或宿主的计数，它的 hook 派的活、发的消息就会按别人的键、按到达的先后
        # 取 id；同理也继承了别人的当前 span，以及别人所在的轨迹窗口。
        becoming_owner(('agent', agent_id), slot.issued)
        outside_any_span()
        _no_windows()
        if not await self.registry.compare_and_set_status(
            agent_id, expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        ):
            await self._report_lost_takeover(agent_id)
            return
        slot.owned = True
        cancelled = False
        try:
            await self._cycles(agent_id, agent, seed, slot)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception(
                '期望: agent %s 的事件循环正常结束。'
                ' 实际: 它以异常结束，Context 的 stop_reason 已置为 error，循环不再继续。'
                ' 下一步: 读 wait() 交回的 Context 定位最后一轮；'
                '要让它自愈就挂一个 llm_failure / post_tool_use hook。',
                agent_id,
            )
        finally:
            if not cancelled:
                await self._finish(agent_id, slot, asyncio.current_task())

    async def _report_lost_takeover(self, agent_id: str) -> None:
        """IDLE -> RUNNING 被拒的两种情形，下一步不是同一件事。

        条目已被注销时，「用 find() 读它的 status 与 owner」答 `None`——读不出任何东西，而本进程
        还留着它的 slot 与实例（`despawn` 收得掉，它的痕迹检查看的是全部痕迹）。措辞相同的一条
        记录把这两种情形合并，就等于在其中一种上给了一个够不着的下一步。
        """
        if await self.registry.get(agent_id) is None:
            logger.error(
                '期望: agent %s 在注册表里是 IDLE，可由本进程接管。'
                ' 实际: IDLE -> RUNNING 被拒，因为条目已经不在注册表里——'
                '它在驱动任务跑起来之前就被销毁了（一条 despawn 级联，或别的执行体注销了它）。'
                ' 下一步: 它已经不可寻址，本进程不跑它；'
                '用 despawn() 收掉本进程为它留下的 slot 与实例，要再跑就重新 spawn 一个。',
                agent_id,
            )
            return
        logger.error(
            '期望: agent %s 在注册表里是 IDLE，可由本进程接管。'
            ' 实际: IDLE -> RUNNING 被拒，它当前的状态不是 IDLE，说明已被别人拿走。'
            ' 下一步: 用 find() 读它的 status 与 owner；本进程不跑它，也不清理它。',
            agent_id,
        )

    async def _report_lost_zombie(self, agent_id: str) -> None:
        """活状态 -> ZOMBIE 被拒的两种情形，下一步不是同一件事。

        条目已被注销时，「用 find() 读它当前的状态」答 `None`——这是 kill 立刻返回、收尾在后台、
        再 despawn 的正常顺序，不是丢失。措辞相同的一条 ERROR 把这两种情形合并，就等于在正常路径
        上稳定误报。
        """
        if await self.registry.get(agent_id) is None:
            logger.debug(
                '期望: agent %s 在事件循环退出时仍可由本进程转去 zombie。'
                ' 实际: 条目已经不在注册表里——它已经被 despawn 掉了。'
                ' 这是 kill 立刻返回、收尾在后台、再 despawn 的正常顺序，不是丢失。'
                ' 下一步: 不用找它；find() 会返回 None。',
                agent_id,
            )
            return
        logger.error(
            '期望: agent %s 在注册表里仍是本进程认得的活状态，可由本进程转去 zombie。'
            ' 实际: 转换被拒，它的状态在事件循环退出前被别人改成了别的东西。'
            ' 下一步: 用 find() 读它当前的状态；本进程已经不再驱动它。',
            agent_id,
        )

    async def _cycles(
        self, agent_id: str, agent: BaseAgent, seed: _Seed | None, slot: _Slot
    ) -> None:
        """IDLE↔RUNNING：等一条消息，跑一个周期，再等下一条。"""
        while True:
            if seed is None and slot.control.deferred_count == 0:
                # 这三句之间没有挂起点，因此它们对控制面是一次原子的「我要去停在邮箱上了」：
                # 到这里手上已经没有在飞的轮，一个还没被消费的中止位没有东西可停，作废它；
                # 紧随其后的 `_oscillate` 把注册表从 ABORTING 带回停泊态。置位在两个 await
                # **之前**，`abort` 因此不会在这段窗口里写下一个没人会取走的 ABORTING。
                slot.control.take_abort()
                slot.parked_on_mailbox += 1
                try:
                    await self._oscillate(agent_id, new=_parked_status(slot))
                    # timeout=None 是刻意的：这个循环的退出路径是取消（kill / shutdown）与队列
                    # 消失，不是超时。答 False 只有一个原因——邮箱已经被摘掉了。
                    if not await self.broker.wait_for_messages(agent_id, None):
                        return
                finally:
                    slot.parked_on_mailbox -= 1
            if await slot.control.hold_until_runnable():
                # 中止落在周期之间：这一轮不开始，位已消费，回去继续等。
                seed = None
                continue
            # 挂起窗口或并发的 despawn 可能已经把邮箱摘掉：此刻开周期就会写下一块没有前置
            # user 块的 assistant 块。回去等，让「邮箱没了」在它唯一的观测点上被答出来。
            if seed is None and not await self._has_unhandled_input(agent_id, slot):
                continue
            await self._oscillate(agent_id, new=AgentStatus.RUNNING)
            slot.gate.begin_cycle()
            slot.control.enter_run()
            try:
                if seed is None:
                    await agent.run(None)
                else:
                    await agent.run(seed.text, trust=seed.trust)
            finally:
                slot.control.leave_run()
            seed = None
            # 预算耗尽这条路无条件退出，即便还有已收下的输入：没有轮次可用，再开一个周期只会
            # 被同一条消息反复唤醒又立刻返回。丢下的那些由 `_finish` 记成 ERROR。
            if agent.context.stop_reason is StopReason.MAX_TURNS:
                return
            if slot.exit_mode is not ExitMode.IDLE and not await self._has_unhandled_input(
                agent_id, slot
            ):
                return

    async def _has_unhandled_input(self, agent_id: str, slot: _Slot) -> bool:
        """还有已经收下、却还没被处理的输入吗？

        邮箱里剩下的消息与暂存的延迟消息是同一件事的两个落点，两边的发送方都拿到过 `ok=True`。
        """
        if slot.control.deferred_count:
            return True
        return await self._has_queued_message(agent_id)

    async def _oscillate(self, agent_id: str, *, new: AgentStatus) -> None:
        """周期边界上的状态转换（停在邮箱上、开周期，见 `_parked_status`）。

        失败不改变循环的走向，但必须留痕。

        `allowed` 是除目标状态之外的全部活状态：控制面在周期之间能留下的每一个状态都在内，所以
        被拒只剩一种解释——条目已被摘掉，或已被别人送进终态。
        """
        allowed = tuple(status for status in _LIVE_STATUSES if status is not new)
        if not await self._switch(agent_id, new=new, allowed=allowed):
            logger.error(
                '期望: agent %s 在周期边界上仍是本进程认得的活状态，可由本进程转去 %s。'
                ' 实际: 转换被拒——它现在既不是目标状态，也不在本进程认得的活状态里，'
                '说明注册表条目已被摘掉或已被别人送进终态，而本进程的事件循环还在跑。'
                ' 下一步: 用 find() 读它当前的状态；要真的停下这个循环就 kill 它。',
                agent_id,
                new.value,
            )

    async def _switch(
        self, agent_id: str, *, new: AgentStatus, allowed: tuple[AgentStatus, ...]
    ) -> bool:
        """把状态 CAS 到 `new`；当前已经是 `new` 也算成功（后置条件已成立）。

        逐个试 CAS 而不是「读一次再决定写什么」：读与写之间的窗口正是 CAS 存在的理由，远端实现
        下另一个执行体可以在这个窗口里改掉状态，而那次转换会被无声吞掉。
        """
        for expected in (new, *allowed):
            if await self.registry.compare_and_set_status(agent_id, expected=expected, new=new):
                return True
        return False

    async def _finish(self, agent_id: str, slot: _Slot, driver: asyncio.Task[Any] | None) -> None:
        """事件循环退出后的归宿（spec §4.1 的 ExitMode）。`driver` 是刚停下的那个驱动任务。

        **状态与资源的拆除是幂等的**：`kill` 的收割任务与驱动任务自己的 finally 都可能走到这里，
        而两者谁先谁后取决于取消是否赶上。轨迹的快照因此只取第一次：第二次可能落在复活之后。
        """
        agent = self._table.get(agent_id)
        if driver is not None and agent is not None and driver not in self._ended:
            self._ended[driver] = _frozen(agent.context)
        await self._report_abandoned_input(agent_id, slot)
        await self.broker.remove_queue(agent_id)
        # 邮箱里那些随队列一起没了，不会再被消费；暂存在控制块里的随唤醒注入，留着它们的来源。
        self._tracing.forget_receiver(agent_id, keep=slot.control.deferred_ids())
        # 周期收尾存一次会话（spec §15）：轨迹此刻成对、工具段已闭合，是恢复要续上的那个干净点。
        # 排在拆树之前，EXIT 的 Agent 也留下最后一份；存盘失败记 ERROR 不抛，不让收尾崩在存盘上。
        await self._checkpoint(agent_id)
        if slot.exit_mode is ExitMode.EXIT:
            await self._destroy_tree(agent_id, operation='exit')
            return
        if not await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=_LIVE_STATUSES):
            await self._report_lost_zombie(agent_id)

    async def _report_abandoned_input(self, agent_id: str, slot: _Slot) -> None:
        """摘队列之前，把「拿过 ok=True 收据、却不会再被处理」的输入记下来。

        循环只在没有未处理输入时退出，所以这条日志出现即意味着一次真的丢失：预算耗尽、周期抛
        异常，或摘队列与最后一次门检查之间的那个窗口。暂存的那些还留在控制块里（slot 在 ZOMBIE
        之后不摘），邮箱里的随队列一起没了。
        """
        stashed = slot.control.deferred_count
        queued = await self._has_queued_message(agent_id)
        if not stashed and not queued:
            return
        logger.error(
            '期望: agent %s 的事件循环退出时，已经收下的输入都已处理——'
            'publish 答过 ok=True 的消息不该随队列一起消失。'
            ' 实际: 还剩 %d 条暂存的延迟消息，邮箱非空=%s；队列即将被摘掉，'
            '邮箱里那些就此丢失，暂存的那些留在控制块里但这个循环不会再读。'
            ' 下一步: 读 wait() 交回的 Context 看它停在哪一轮（预算耗尽就提高 max_turns），'
            '再把这些消息重投一次——在发送方看来它们是投递成功的。',
            agent_id,
            stashed,
            queued,
        )

    async def _has_queued_message(self, agent_id: str) -> bool:
        """邮箱此刻非空吗。

        `timeout=0` 是纯轮询（见 `MessageBroker.wait_for_messages`）：它不取走消息，因此不与门
        那唯一一条消费路径分叉；也不构成一次「停在邮箱上等」。
        """
        return await self.broker.wait_for_messages(agent_id, 0)

    async def wait(self, agent_id: str) -> Context:
        """类比 waitpid：等事件循环结束，交回它的轨迹。

        交回的是 Agent 自己那份 Context，不是快照：ZOMBIE 靠一条消息复活时续用的就是它，拿到副本
        的调用方会与 Agent 将来读写的那一份静默分叉。

        **`ExitMode.IDLE` 的 Agent 事件循环常驻**，因此 `wait` 只在它被 shutdown / kill / despawn
        之后才返回——对守护型 Agent 调 `wait` 等于故意把自己挂住。

        Agent 的异常与取消都是**被等待者**的终态，不冒充成本次调用自己的失败：崩溃在
        `ctx.stop_reason` 上是 ERROR，被取消的运行没有 stop_reason。
        """
        task = self._table.get_task(agent_id)
        if task is not None:
            await asyncio.wait({task})
        return self._finished_context(agent_id)

    def _finished_context(self, agent_id: str, driver: asyncio.Task[Any] | None = None) -> Context:
        """一个事件循环已经停下的 Agent 的轨迹。`wait` 与 `_wait_in_turn` 的共同出口。

        两条路各写一份，迟早会在「接管失败算不算等到了」这种地方分叉，而分叉出来的差异只在
        接管真的失败时才现身。

        给了 `driver` 就交回那个驱动任务停下时的快照（见 `_ended`）。还没有快照说明 `_finish`
        还没开始，它就还进不了 zombie、没有被复活过，活的那份就是停下时的那份。
        """
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            raise self._nothing_to_wait_for(agent_id)
        if not slot.owned:
            raise ContractViolation(
                expected=f'agent {agent_id!r} 的事件循环由本进程接管过',
                received=(
                    '本进程 spawn 过它，但驱动任务没有接管成功：'
                    'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                ),
                next_action='用 find() 读它当前的 status 与 owner；确认它空闲后重新 spawn 一个',
            )
        if driver is not None:
            ended = self._ended.get(driver)
            if ended is not None:
                return ended
        return agent.context

    async def _checkpoint_all_sessions(self) -> None:
        """关机前把所有会话根 Agent 各存一次。`_checkpoint` 自带 `session_id` 为空就跳过的判定，
        因此这里对表里每个 Agent 都调一遍；root-only 下至多一个会话根，开销可忽略。"""
        if self._sessions is None:
            return
        for agent_id in list(self._table.agent_ids()):
            await self._checkpoint(agent_id)

    async def _checkpoint(self, agent_id: str) -> None:
        """把一个会话根 Agent 的当前轨迹存进 SessionStore（spec §15 的会话持久化）。

        **内部调用，不挂 observable 钩子**：存盘失败是宿主要知道的运维事件，而钩子机制会把回调
        抛的异常整个丢弃（见 hooks.center 文档）。因此失败在这里记 ERROR 并发一条事件，既不静默
        吞、也不往上抛——收尾路径崩在存盘上会连累同一棵树里别的 Agent 的拆除。

        只存 `session_id` 非空的 Agent（宿主把它标成会话根，约定 sid = session_id）；没配
        SessionStore 时直接返回。落盘前过 Runtime 持的 Redactor（spec §12 的落盘收口）。
        """
        store = self._sessions
        if store is None:
            return
        agent = self._table.get(agent_id)
        if agent is None:
            return
        info = await self.registry.get(agent_id)
        if info is None or info.session_id is None:
            return
        sid = info.session_id
        phase = 'build'
        try:
            record = await self._session_record(store, sid, agent, info)
            phase = 'redact'
            redacted = redacted_record(record, self._redactor)
            phase = 'save'
            await store.save(sid, redacted)
        except Exception as error:
            # 分阶段报：说清哪一步（拼记录 / 脱敏 / 落盘）失败、异常类型，别把 AttributeError 或坏
            # extra 一律误报成「磁盘满」（F8）。记录与事件都不静默吞，也不连累收尾。
            logger.error(
                '期望: 会话 %s 在周期收尾时存盘成功。'
                ' 实际: %s 阶段抛了 %s：%s。'
                ' 下一步: save 阶段查后端是否可写（磁盘满 / 权限 / 远端不可达）；build / redact'
                '阶段多半是数据或代码问题（坏 extra、脱敏命中结构字段）。这次丢了、恢复回到上个存过'
                '的点，在途改动不受影响。',
                sid,
                phase,
                type(error).__name__,
                error,
            )
            self._tracing.event(
                'session.checkpoint_failed',
                {
                    'session.id': sid,
                    'agent.id': agent_id,
                    'phase': phase,
                    'error': type(error).__name__,
                },
            )

    async def _session_record(
        self, store: SessionStore, sid: str, agent: BaseAgent, info: AgentInfo
    ) -> SessionRecord:
        """从一个存活 Agent 与它的登记信息拼一份 SessionRecord。

        `created_at` / `title` / `archived` / `last_trace` 从已存的元数据接着用：`save` 覆盖整条
        记录，不接住这几个由更早的存盘或一次 archive 定下的字段，每次收尾就会把它们清掉。
        """
        existing = await store.load_meta(sid)
        now = time.time()
        meta = SessionMeta(
            sid=sid,
            title=existing.title if existing is not None else '',
            agent=info.name,
            status=SessionStatus.ACTIVE,
            turns=agent._current_turn,  # pyright: ignore[reportPrivateUsage]
            archived=existing.archived if existing is not None else 0,
            last_trace=existing.last_trace if existing is not None else None,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return SessionRecord(
            meta=meta,
            content=agent.context.current_traj,
            system_prompt=agent.get_system_prompt(),
            model=agent.options.llm.config.model,
            tools=list(agent.options.tools),
            flags=dict(info.flags),
        )

    async def _wait_in_turn(self, agent_id: str, target_id: str) -> _InTurnWaitResult:
        """代表一个正在干活的 Agent 等另一个 Agent 的事件循环结束（供编排工具使用）。

        **私有**：唯一的消费者是本包里的 `Delegate` / `Fork`，理由同 `_InTurnWaitResult`。

        与 `wait` 的区别只有一条：**这次等待打断得了**。`wait` 从 Runtime 外面调，调用方自己套
        取消；这里从一次工具调用内部发起，此刻 `agent_id` 正卡在一轮的中间——而中止位
        只在轮的边界上被读到，不打断这次等待，一次 `abort(agent_id)` 要等被等的那个 Agent 自己
        干完才兑现。这与 `wait_for` 上那条裁决同型，令牌也是同一个机制。

        `SafetyModule.max_wait_seconds` 为正时，本等待与 `wait_for` 受同一宿主级墙钟上限约束；默认
        0 不凭空截短合法长任务。到期只让发起方收到 TIMEOUT，不取消被等 Agent。

        **注册表不写 `WAITING`。** `WAITING` 说的是「这一轮悬在一条匹配的答复上」，而等一个子
        Agent 干活是这一轮的工作本身；写下它会让 `resume` 的 CAS（只收 `PAUSED`）与
        `_resumed_status` 一起把一个正在往前走的 Agent 报成在等答复。

        **一个 Agent 同时只有一次在飞的在轮等待，新的顶掉旧的**（理由同 `wait_for`：被顶掉的
        那次工具调用必须能返回点什么，而「一次调用恰好一个结果块」是承重不变式）。

        **被等的那一方真的结束了就交回它的轨迹，即便中断也已经到达。** 此刻丢掉一份已经跑完的
        结果没有任何好处，而中止仍会在下一个轮边界上兑现。

        **目标的两道守卫都在装令牌之前**：等自己是永久挂死，而目标不存在时这一轮悬在一次没有任何
        东西能解开的等待上。被拒的调用因此不在这个 Agent 的槽里留下任何痕迹。

        span 的结构键按出处里的第几次等待（M3 D3），在入口就占用：「目标还在不在」取决于调度，
        被拒的这一次也要占掉序号。
        """
        wait_key = next_key('wait')
        slot = self._slots.get(agent_id)
        if slot is None:
            raise _cannot_wait_in_turn(
                agent_id, '本进程没有它的实例——它由别的进程持有，或它已被销毁'
            )
        if not slot.owned:
            raise _cannot_wait_in_turn(
                agent_id,
                '本进程 spawn 过它，但驱动任务没有接管：'
                'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                '——本进程手上没有一轮可以挂起',
            )
        if target_id == agent_id:
            raise _cannot_wait_on_itself(agent_id)
        if target_id not in self._slots or self._table.get(target_id) is None:
            raise _unknown_in_turn_target(agent_id, target_id, sorted(self._slots))
        with self._wait_span(agent_id, wait_key, 'agent', target_id) as span:
            result = await self._waited_in_turn(slot, target_id)
            span.attributes['wait.outcome'] = result.state.value
            return result

    async def _waited_in_turn(self, slot: _Slot, target_id: str) -> _InTurnWaitResult:
        token = _WaitToken()
        previous = slot.delegation
        slot.delegation = token
        if previous is not None:
            previous.interrupt(WaitState.PREEMPTED)
        if slot.control.abort_requested:
            # 装上令牌之后复检：中止可能落在取出槽位与装上之间，而那时 `_abort_one` 看不到还没
            # 装上的令牌，于是这次等待会一直挂到被等的那一方自己结束——中止的整个意义就没了。
            # **只读不取**：位由轮边界的 `hold_until_runnable` 消费，在这里取走它，这一轮就不会
            # 以 ABORTED 收场了。
            token.interrupt(WaitState.ABORTED)
        try:
            target = self._table.get_task(target_id)
            if target is not None and not target.done():
                # 只等、不取消：`target` 是被等那一方的驱动任务（取消它就是一次没人要求的
                # `kill`，而这次等待被打断不该改变被等那一方的归宿），`token.raised` 归令牌所有
                # （取消它会让此后的 `interrupt` 静默落空）。
                await asyncio.wait(
                    {target, token.raised},
                    timeout=_wait_ceiling(slot.flags),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            # 「被等的那一方结束了没有」只判这一次，两条路（等过与没等过）因此给出同一个答案，
            # 而中断胜出只是**它没结束**的那一格。写成「令牌被打过就交回中断」会让一份已经跑完的
            # 轨迹在中断也到达时被丢掉。
            if target is None or target.done():
                return _InTurnWaitResult(
                    state=WaitState.ANSWERED, context=self._finished_context(target_id, target)
                )
            if token.raised.done():
                return _InTurnWaitResult(state=token.raised.result())
            return _InTurnWaitResult(state=WaitState.TIMEOUT)
        finally:
            # `is token`：被顶掉的那次等待先返回时，槽里装的是顶掉它的那个令牌，而那次等待还挂着。
            if slot.delegation is token:
                slot.delegation = None

    def _nothing_to_wait_for(self, agent_id: str) -> ContractViolation:
        return ContractViolation(
            expected=f'agent {agent_id!r} 由本进程 spawn，且结束后仍留在本进程里',
            received=(
                f'本进程没有它的运行记录（已知: {sorted(self._slots)}）：'
                '要么它不是本进程 spawn 的，'
                '要么它以 ExitMode.EXIT 结束，已连同 Context 一起被销毁'
            ),
            next_action=(
                '确认 agent_id 是 spawn 的返回值；'
                '要在结束后读轨迹就用 ExitMode.ZOMBIE 或 ExitMode.IDLE spawn；'
                '若它由其他进程持有，本进程无法 wait 它'
            ),
        )

    async def send(
        self,
        agent_id: str,
        content: str,
        *,
        trigger: Trigger = Trigger.PENDING,
        activate: bool = True,
        sender: str = 'runtime',
        correlation_id: str | None = None,
        trust: TrustLevel = TrustLevel.TRUSTED,
    ) -> DeliveryResult:
        """投递一条消息到目标邮箱。

        **默认激活目标**：绝大多数调用方要的语义是「发消息，它没在跑就叫醒它」（spec §4.2）。
        要严格的「仅入队」语义就传 `activate=False`——此时目标若已经进了 ZOMBIE（队列已摘），
        投递返回 `ok=False`，而不是把消息塞进一个没人消费的地方。

        `trust` 是这段 `content` 的信任级别（spec §12.2 第 2 条），由**写下这段文字的那一方**给
        出，经 `Message.trust` 与 `BaseAgent._inject` 原样落进收件人的 user 块。默认可信：宿主自己
        写的消息就是可信的。**内容来自一个模型时必须传它**——模型读过什么决定了那段文字是什么的
        衍生物，标记丢在这里，收件人的高风险调用就绕过了宿主配置的升级处置（`SendMessage` 传的
        是发送方的溯源结论）。

        **投递失败返回结构化结果而不抛异常**，调用方才能识别可重试的瞬时错误。

        可寻址性以注册表为准，不以邮箱在不在为准：残留邮箱（远端 broker 的 remove_queue 失败、
        或 ensure_queue 之后注册失败）会让投递看起来成功，而再没有人会去消费它。
        """
        # 键在查注册表之前占用：收件人此刻还在不在取决于调度（它可能正好退出），投不出去的这一条
        # 也要占掉它的序号，后面的消息才在两次运行里拿到同一个键（M3 D10）。
        trust = self._from_listed_module(trust)
        key = self._message_key(agent_id, correlation_id)
        if await self.registry.get(agent_id) is None:
            return DeliveryResult(
                ok=False,
                reason=(
                    f'期望: agent {agent_id!r} 已注册，因而可寻址。'
                    ' 实际: 注册表里没有这个条目。'
                    ' 下一步: 先 spawn 它；以 ExitMode.EXIT 结束的 Agent 已被销毁，只能重新 spawn。'
                ),
            )
        message = Message(
            sender=sender,
            receiver=agent_id,
            content=content,
            trigger=trigger,
            correlation_id=correlation_id,
            trust=trust,
            id=_fresh_id(self._ids.message_id, key, 'message_id'),
        )
        self._tracing.sending(message.id, agent_id)
        token = _ACTIVATION_ALLOWED.set(activate)
        delivered = None
        try:
            delivered = await self.broker.publish(agent_id, message)
            return delivered
        finally:
            _ACTIVATION_ALLOWED.reset(token)
            self._tracing.sent(
                message.id, agent_id, delivered=delivered is not None and delivered.ok
            )

    async def ensure_active(self, agent_id: str) -> bool:
        """唯一的激活入口：复活 ZOMBIE、重开事件循环、从落盘会话恢复（spec §4.2）。

        复活、重开、从落盘会话恢复、恢复子会话在语义上是同一件事，不该暴露成四个方法让用户猜。

        返回 True 表示「此刻它有一个活着的事件循环」，False 表示本进程做不到——**两者都是回答，
        都不抛异常**（问句只答不抛）。三种本进程无法自助诊断的 False 各记一条日志：手里没有它的
        实例、它正在收尾、状态转换被拒。注册表里没有它不记——那与 `find` 答 None 是同一个正常
        答案，而且「这个 Agent 不归我」正是回调链上设计好的一支，记下来只会让每一次转手都报错。
        """
        return await self._ensure_active(agent_id, resume_from_store=True)

    async def _ensure_active(self, agent_id: str, *, resume_from_store: bool) -> bool:
        """`ensure_active` 的内核：`resume_from_store` 决定注册表里没有它时要不要从落盘会话恢复。

        显式 `ensure_active`（恢复入口）为真；`_on_missing_queue`（send 懒激活）为假——一条发给只剩
        落盘会话的 id 的散落消息不该把整份会话重建起来，恢复是 CLI 显式发起的动作。于是 send 那条路
        对只在磁盘上的会话一次 store 都不碰（Resume contract）。
        """
        if self._closed:
            return False
        info = await self.registry.get(agent_id)
        if info is None:
            return await self._resume_from_store(agent_id) if resume_from_store else False
        slot = self._slots.get(agent_id)
        agent = self._table.get(agent_id)
        if slot is None or agent is None:
            logger.error(
                '期望: agent %s 的实例在本进程的实例表里，可以直接重开它的事件循环。'
                ' 实际: 注册表里有它，本进程手里没有——它由别的进程持有，'
                '或者只剩落盘会话而恢复能力尚未实现。'
                ' 下一步: 在持有它的进程上激活它；'
                '要在本进程续跑就重新 spawn 一个并把会话交给它。',
                agent_id,
            )
            return False
        if self._has_live_driver(agent_id):
            return True
        if info.status is AgentStatus.ABORTING:
            logger.error(
                '期望: agent %s 处在可以重开事件循环的状态。'
                ' 实际: 它正在收尾（aborting），此刻重开会与收尾抢同一份轨迹。'
                ' 下一步: 等它进 zombie 之后再激活；用 find() 读它当前的状态。',
                agent_id,
            )
            return False
        if not await self._switch(agent_id, new=AgentStatus.IDLE, allowed=(AgentStatus.ZOMBIE,)):
            logger.error(
                '期望: agent %s 在注册表里是 idle 或 zombie，可由本进程重开事件循环。'
                ' 实际: 转换被拒，本进程读到它是 %s，而本进程手里的驱动任务已经结束——'
                '它由别人在跑，或已被送进别的状态。'
                ' 下一步: 用 find() 读它当前的状态；条目已被 despawn 就只能重新 spawn。',
                agent_id,
                info.status.value,
            )
            return False
        await self.broker.ensure_queue(agent_id)
        # 上面那次 has_live_driver 之后隔着两个挂起点，并发的另一次激活可能已经把驱动挂上去了。
        # 复检与 create_task / set_task 之间没有挂起点，这三步因此对事件循环是原子的；少了复检，
        # 第二次会撞上实例表拒绝替换未结束任务，把只答不抛的问句变成抛异常并丢下一个孤儿任务。
        if self._has_live_driver(agent_id):
            return True
        if self._closed or agent_id in self._doomed:
            # ZOMBIE -> IDLE 已经写下去了，而这里不会再有驱动接着：留着 idle 就是宣布「它能处理
            # 消息」，而 zombie 才是复活它的凭据（`ensure_active` 的 `allowed` 只有它）。
            await self._switch(agent_id, new=AgentStatus.ZOMBIE, allowed=(AgentStatus.IDLE,))
            logger.error(
                '期望: agent %s 可以重开事件循环。'
                ' 实际: 复活走到一半时本 Runtime 已经 shutdown、或它已经在被拆——此刻建出来的驱动'
                '没人会取消也没人会 await，只会在解释器退出时变成一行「Task was destroyed but it'
                ' is pending」。'
                ' 下一步: 注册表状态已经写回 zombie；被拆的等它拆完（find() 会答 None），'
                '关机的新建一个 Runtime 重新 spawn。',
                agent_id,
            )
            return False
        # 复活的循环没有 query：它要干什么由那条把它叫醒的消息说，门会在第一轮开始前消费它。
        revived = asyncio.create_task(self._drive(agent_id, agent, None), name=f'agent:{agent_id}')
        self._table.set_task(agent_id, revived)
        return True

    async def _resume_from_store(self, sid: str) -> bool:
        """落盘会话恢复（spec §4.2 / §15）：Agent 不在注册表、而 SessionStore 里有它时，从记录重建。

        复用 spawn 的全部装配 / 回滚 / 并发收口——工厂包一层把 id 定成 sid（Agent 的 id 取自
        `assigning_agent_id`，spawn 以 `agent.agent_id` 为准），构造时种回轨迹与 turn 计数。按
        `SessionMeta.agent` 从已登记的角色工厂解析，缺了给三件套错误。安全相关 flags 用当前模块值、
        不采信快照（P22）；model / tools 与快照不一致记 warning、按当前值续跑（接受一次缓存失配）。
        """
        store = self._sessions
        if store is None:
            return False
        try:
            if not await store.exists(sid):
                return False
            record = await store.load(sid)
        except Exception:
            # 「只答不抛」（spec §4.2）：坏 id、坏记录、后端出错都记一条 ERROR、答 False，不往上抛。
            logger.exception(
                '期望: 恢复会话 %s 时 SessionStore 可读。'
                ' 实际: exists / load 抛了异常（坏 id、坏记录、或后端不可达）。'
                ' 下一步: 看 .syndica/sessions 下这个会话是否完好；坏了就 sessions rm 再重来。',
                sid,
            )
            return False
        if record is None:
            return False
        factory = self._agents.get(record.meta.agent)
        if factory is None:
            logger.error(
                '期望: 恢复会话 %s 时角色 %r 在已登记的角色工厂里。'
                ' 实际: 这个 Runtime 没登记这个角色——它由另一套配置写下，或角色被改名 / 删掉。'
                ' 下一步: 用 Runtime(agents=...) 或 assemble 登记同名角色再恢复。',
                sid,
                record.meta.agent,
            )
            return False
        # 整份轨迹的溯源（截断前就取，防御性保留）：挂到 resume_note 上，恢复后 provenance 不被洗。
        full = Context()
        for block in record.content:
            full.append(block)
        carried = provenance_of(full)
        # **种回原始轨迹，不截末尾未配对的批次**：Ctrl-C / SIGTERM 多半停在工具批次中间，存下的尾部
        # 是一个已发起（可能已部分执行）的调用批次加已返回结果。`_forkable_prefix` 截掉它会抹掉「副
        # 作用发生过」，模型会重放非幂等操作。原样种回，`_repair_interrupted_batch` 把未配对调用
        # 补成「副作用未知」、保住已返回的结果（评审 H4）。
        seeded = list(record.content)
        note = signed_block(
            HistoryKind.RESUME_NOTE,
            '（恢复）从落盘会话续跑：上一段进程里的子 Agent 已随进程结束失效，'
            '别再向它们的 id 发消息。',
            key=f'resume:{sid}:{record.meta.turns}',
            trust=carried,
        )
        turn = record.meta.turns

        def resume_factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            token = assigning_agent_id(sid)
            try:
                agent = factory(hooks, events)
            finally:
                releasing_agent_id(token)
            for block in seeded:
                agent.context.append(block)
            if not has_block(agent.context.current_traj, note.extra[IDEMPOTENCY_KEY]):
                agent.context.append(note)
            agent._current_turn = turn  # pyright: ignore[reportPrivateUsage]
            return agent

        if self._closed:
            # 上面隔着 store 的 await，并发的 shutdown 可能插进来；此刻 spawn 会抛，而这是个问句。
            return False
        try:
            resumed_id = await self.spawn(resume_factory, exit_mode=ExitMode.ZOMBIE, session=True)
        except Exception:
            logger.exception('期望: 恢复会话 %s 时能起驱动任务。 实际: spawn 抛异常。', sid)
            return False
        if resumed_id != sid:
            # 工厂没接住交给它的 id（返回缓存实例 / 先建了别的 Agent）：它被登记到别的 id 下、还带着
            # session_id，会把整份旧轨迹存成一个新会话——拆掉它、答 False，不留影子会话（评审 M2）。
            logger.error(
                '期望: 恢复会话 %s 的角色 %r 用交给它的 id 构造 Agent。'
                ' 实际: 它交回的 Agent id 是 %s，会话会被复制到那个 id 下。'
                ' 下一步: 让该角色的工厂在这次调用里构造并交回 Agent（读 assigning_agent_id）。',
                sid,
                record.meta.agent,
                resumed_id,
            )
            await self.despawn(resumed_id)
            return False
        self._warn_on_prefix_drift(resumed_id, record)
        return True

    def _warn_on_prefix_drift(self, agent_id: str, record: SessionRecord) -> None:
        """恢复按当前角色配置续跑；model / tools 与快照（为缓存连续性而存）不一致时记一条 warning。
        不比 SP：快照里的 SP 已脱敏，与活的 SP 无法逐字比。"""
        agent = self._table.get(agent_id)
        if agent is None:
            return
        drift: list[str] = []
        if agent.options.llm.config.model != record.model:
            drift.append(f'model {record.model!r} -> {agent.options.llm.config.model!r}')
        if list(agent.options.tools) != record.tools:
            drift.append(f'tools {record.tools} -> {list(agent.options.tools)}')
        if drift:
            logger.warning(
                '会话 %s 恢复：前缀快照与当前角色配置不一致（%s）；'
                '按当前值续跑，这次恢复缓存不命中。',                agent_id,
                '; '.join(drift),
            )

    def _has_live_driver(self, agent_id: str) -> bool:
        task = self._table.get_task(agent_id)
        return task is not None and not task.done()

    async def _on_missing_queue(self, agent_id: str) -> bool:
        """回调答的是「邮箱建好了没有」，不是「它活着没有」；两者只在活驱动这一支上分叉。

        活驱动而邮箱不在，只可能落在 `_finish` 摘掉队列之后。此刻把邮箱建回来，消息会落进一个
        再没有消费者的地方；答 True 却没建好，则掐断回调链——本来能把它建起来的下一个回调不会
        被问到。
        """
        if not _ACTIVATION_ALLOWED.get():
            return False
        if self._has_live_driver(agent_id):
            return False
        return await self._ensure_active(agent_id, resume_from_store=False)

    async def pause(self, agent_id: str, *, recursive: bool = False) -> None:
        """挂起：让它在下一个轮的边界上停下来（spec §5.1 第 2 步），注册表转成 PAUSED。

        挂起的那段时间**不计入任何一轮**——门在计数之前，因此挂起既不消耗预算，也不会把一轮切成
        两半。**返回不等于它已经停下来**：turn 循环可能正卡在一次 LLM 调用里，「真的停了」只有
        `ControlBlock.parked` 答得出。

        允许从 IDLE / RUNNING / WAITING 进入；已经是 PAUSED 是成功（后置条件已成立）。ZOMBIE 与
        ABORTING 报错：前者没有事件循环可停，而它的注册表状态正是 `ensure_active` 复活它的凭据，
        写上 PAUSED 它就再也叫不醒；后者正在收尾，挂起一个正在拆的东西没有可表达的意图。

        `recursive=True` 时**父先于子**：父只在下一个轮边界停下，因此它仍可能在遍历期间生出新的
        子代——父先于子把这个窗口从无界收窄到**一次在飞的轮**，并不是消除它。那一轮里出生的子代
        遍历看不到，也就不会被挂起；要一棵完整挂起的子树只能再走一遍。父先于子另外保证：被点名
        的那一个被拒时，异常是在走这棵树**之前**抛出的，不会留下一棵改了一半的树。
        """
        await self._pause_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._pause_one(descendant, named=False)

    async def resume(self, agent_id: str, *, recursive: bool = False) -> None:
        """解除挂起，注册表从 PAUSED 转回它该在的地方。

        **回到哪里由控制块与在飞的等待说了算**：还悬在一次 `wait_for` 上就回 WAITING，否则在一次
        `run()` 之内回 RUNNING、不在则回 IDLE。只有持有事件循环的进程知道这三件事，而注册表上没有
        「挂起之前是什么」——记一份就多出一个必须与真相保持一致的东西，而挂起期间一次 `wait_for`
        的开始与结束都会改动它。

        **门无条件抬起，注册表那一步尽力而为。** 只有本组方法抬得起这道门，因此「记不下就不抬」
        会把这个 Agent 永久停在门上；过时的状态可以用 `find` 重读，没人能抬起的门谁都救不回来。

        `recursive=True` 时**子先于父**：父恢复之后可能立刻要用子代，先恢复子代，它就不会看见
        一棵只恢复了一半的树。
        """
        if recursive:
            for descendant in reversed(await self._descendants(agent_id)):
                await self._resume_one(descendant, named=False)
        await self._resume_one(agent_id, named=True)

    async def abort(self, agent_id: str, *, recursive: bool = False) -> None:
        """协作式中止：置位中止位、把状态转成 ABORTING，然后立刻返回。

        中止**在轮开始前（spec §5.1 第 1 步）和模型已经产出之后、工具执行之前（第 9 步）兑现**：
        这一次工作以 `StopReason.ABORTED` 收场，轨迹配对完整、可以直接 resume。第 9 步保留
        assistant block，并为每个尚未执行的调用补一块如实说明「没有执行」的 tool block。要立刻
        停下、不惜留下不配对的调用，那是 `kill`——两者的区别正是 `ABORTED` 存在的理由，不要合并。

        位是**一次性**的：这一个周期结束，事件循环照常回去等下一条消息（`exit_mode` 决定它还在
        不在）。abort 是「停下手上这一件事」，不是「从此不再干活」。**暂存的延迟消息不在被停的
        范围里**——它们排的是下一件事的队，那一件还没开始。

        **手上没有活时它成功，并且什么都不写。** 事件循环停在邮箱上（IDLE，或挂起着而邮箱空）、
        或已经结束（ZOMBIE），此刻轨迹已经配对完整——那正是协作式中止要保住的东西，后置条件已经
        成立。判据是**驱动任务此刻在哪**，不是注册表说什么：注册表分不出停在邮箱上的 PAUSED 与
        停在挂起门上的 PAUSED，而只有后者收得到唤醒边沿。这条判断在一段不含 await 的代码里做完，
        因此与「一个周期开始」之间没有交错窗口。之后才到达的消息开的是**下一件事**，不在本次调用
        的范围里——它本来也停不住：被中止的周期不 consume 邮箱，那条消息照样换来一个完整的周期。

        因此 abort **不因为「它在做什么」而拒绝**。拒绝只剩下「本进程答不了这个问题」：注册表里
        没有这个条目、本进程手里没有它的实例、驱动任务没有接管过它，以及转 ABORTING 的 CAS 输给
        了并发的写者。

        对 PAUSED 允许，且中止**穿透挂起门**：先挂起再中止是真实顺序，若中止要等解除挂起才被
        看见，这个 Agent 会永远停在那里而调用方毫无反馈。**穿透不等于解除**：门原样落着，兑现
        之后它回到挂起态等 `resume`。

        **同样穿透一次在飞的 `wait_for`**，那次等待当场以 `WaitState.ABORTED` 返回：中止位在轮
        开始前和第 9 步被读到，而等待卡在轮的中间，不打断它就要等那次等待自己超时才兑现。

        `recursive=True` 时**父先于子**，理由同 `pause`：先掐掉还会派出新活的那一头。父只在下一个
        轮边界停下，因此它仍可能在遍历期间生出新的子代——父先于子把这个窗口收窄到**一次在飞的
        轮**，并不是消除它。要一棵完整中止的子树只能再走一遍。
        """
        await self._abort_one(agent_id, named=True)
        if recursive:
            for descendant in await self._descendants(agent_id):
                await self._abort_one(descendant, named=False)

    async def kill(self, agent_id: str) -> None:
        """硬终止：取消事件循环并**立刻返回**，收尾在后台完成。

        spec §4.1 给 ABORTING 的理由就是这个——它是中间态，好让 `kill` 不必等收尾。

        取消可能落在轮的中间，因此这条路**不产生 `stop_reason`**，并且会留下一个声明了 N 个调用却
        不足 N 条结果的 assistant block。那不是缺陷：`BaseAgent.run` 在入口按「中断、副作用未知」
        把它补成配对完整的轨迹，于是被杀掉的 Agent 仍然可以被一条消息复活并继续干。要一次配对完整、
        带 `StopReason.ABORTED` 的收场，那是 `abort`——两者的区别正是 `ABORTED` 存在的理由。

        **`kill` 自己不注销注册表条目**，它把 Agent 送进 ZOMBIE：条目还在，因此它的子代不会指向一
        个已被删掉的父，`kill` 也就不需要级联。条目的去留仍由 `exit_mode` 决定——`ExitMode.EXIT` 的
        Agent 走的是同一条级联拆除，条目连同整棵子树一起消失。要无条件连条目一起去掉是 `despawn`。

        **刻意不置中止位。** 取消已经足够；而中止位是会留下来的状态，此后一次 `ensure_active` 复活会
        在第一轮就把它消费掉，让复活出来的 Agent 立刻结束——一次 kill 变成了永久的诅咒。

        `shutdown` 之后是成功的 no-op：事件循环那时都已经停下（后置条件已成立），而收尾与 `shutdown`
        自己一样不做；此刻再排一个收割任务，那是个永不被 await 的孤儿。
        """
        slot, info = await self._control_target('kill', agent_id, named=True)
        if slot is None or info is None:
            return
        if self._closed:
            return
        # 转换失败不记：那只意味着它已经进了终态（killing 一个 zombie 是成功的 no-op），而真正
        # 要保证的归宿由收割任务给出，那一步失败会自己留痕。
        await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES)
        # 复检与 create_task 之间没有挂起点：并发的 shutdown 已经等过收割任务，此刻再建一个就是
        # 永不被 await 的孤儿。
        if self._closed:
            return
        task = self._table.get_task(agent_id)
        if task is not None and not task.done():
            task.cancel()
        # 已经有一次收尾在飞就不再排一个：第二个收割任务会把同一次收尾整个走第二遍。归宿由那一次
        # 在飞的收尾给出，而 `kill` 承诺的只是**立刻返回**、不承诺归宿已经到达——所以这里不是
        # 「后置条件已成立」，而是「这件事已经有人在做，且它失败会自己记一条 ERROR」。
        if agent_id in self._reaping:
            return
        self._reaping.add(agent_id)
        reaper = asyncio.create_task(self._reap(agent_id, slot), name=f'reap:{agent_id}')
        self._reapers.add(reaper)
        reaper.add_done_callback(self._reapers.discard)

    async def despawn(self, agent_id: str) -> None:
        """销毁：连注册表条目一起去掉，**并且级联到全部子代**。

        级联不是可选项，所以这里没有 `recursive` 开关：**要么条目还在，要么它和它的子树一起不在**
        （理由见模块文档）。拆除顺序是**子先于父**——任何一刻都不存在「父已经没了而子还活着」的中
        间态。

        **不因为「本进程没接管过它」而拒绝。** 树在共享注册表里，而 `pause` / `resume` / `abort`
        只作用于本进程按得住的 Agent；`despawn` 照搬那条规则就会留下一个别人持有的子代，它的
        `parent_id` 指向一个已被注销的父。因此本方法作用在**条目**上，不作用在事件循环的所有权上。

        `despawn` 等**本进程持有的**每一个事件循环真的停下来再返回：不等到，「这些条目都不在了」
        连问都问不出来。别的进程持有的那些停不了，它们自己会在下一个周期边界发现条目已经不在。要
        立刻返回就用 `kill`。

        **拆除是尽力而为的**：某个条目注销不掉（远端注册表抖动、`unregister` 抛，或它返回了却没
        真的把条目摘掉）时，那个条目留在注册表里、其余照拆。此后它的父已经注销，它的 `parent_id`
        指向一个不存在的条目——那条记录里的下一步（按 agent_id 单独 despawn 它）是够得着它的唯一
        办法。所以返回不等于「这棵子树一个条目都不剩」，要确认就用 `find`。

        **但返回意味着二者之一**：这棵子树一个条目都不剩，或者留下的每一个都记了一条 ERROR。
        取消不在这个承诺里——被取消的 `despawn` **不返回**，`CancelledError` 一路上抛。

        未知 agent 是成功的 no-op：「它不在注册表里」这个后置条件已经成立。
        """
        self._refuse_self('despawn', agent_id)
        if agent_id not in self._slots and await self.registry.get(agent_id) is None:
            return
        await self._destroy_tree(agent_id, operation='despawn')

    async def _destroy_tree(self, agent_id: str, *, operation: str) -> None:
        """`kill` 的收尾、`despawn` 与 `ExitMode.EXIT` 共用的唯一拆除顺序。

        **先把整棵子树钉住，再拆**（理由见模块文档）。反复走而不是走一遍：第一遍的 `children_of`
        与钉住之间有窗口，那一刻出生的子代只有下一遍才看得见；钉住之后树上再挂不进新子代，所以走到
        没有新节点为止这棵树的形状就冻住了。

        拆除顺序是**发现顺序的逆序**：广度优先只能经由父发现子，因此逆序里子一定在父之前被拆。

        遍历到的子代拆不掉只记日志、不中断：递归操作是尽力而为的，为第一个冲突抛异常会留下一棵改了
        一半的树，而调用方无从得知改到哪了。被点名的那一个照常抛。
        """
        descendants: list[str] = []
        seen = {agent_id}
        self._pin(seen)
        try:
            while fresh := [
                found for found in await self._descendants(agent_id) if found not in seen
            ]:
                seen.update(fresh)
                descendants.extend(fresh)
                self._pin(fresh)
            # 抛在动手之前：等自己就是永久挂死，而半棵拆掉的树没有人能收拾。
            for descendant in descendants:
                self._refuse_self(operation, descendant)
            for descendant in reversed(descendants):
                try:
                    await self._destroy(descendant)
                except Exception:
                    logger.exception(
                        '期望: agent %s 作为 %s 的子代被拆掉，条目随子树一起消失。'
                        ' 实际: 拆它时抛了异常，条目留在注册表里，而它的父即将被注销——'
                        '它的 parent_id 从此指向一个不存在的条目，children_of 再也找不到它。'
                        ' 下一步: 用 find() 确认它还在，再按 agent_id 单独 despawn 它；'
                        '它的父已经不在，只能这样够得着它。',
                        descendant,
                        agent_id,
                    )
            await self._destroy(agent_id)
        finally:
            self._unpin(seen)

    def _pin(self, agent_ids: Collection[str]) -> None:
        """记下「本次级联把这些条目钉住了」，每个加上本次调用自己的那一份。

        形参是 `Collection` 而不是 `Iterable`：`_pin` 与 `_unpin` 必须走**同一批** id，而单趟
        迭代器会被 `_pin` 耗干、`_unpin` 一个都减不到，而失衡的表现是这棵子树永久挂不上新子代。
        """
        for agent_id in agent_ids:
            self._doomed[agent_id] = self._doomed.get(agent_id, 0) + 1

    def _unpin(self, agent_ids: Collection[str]) -> None:
        """只撤掉本次调用放上去的那一份，别人的留着。

        写在 `finally` 里的释放**也在一个节点都没拆就退出时执行**（预检查抛、遍历抛），所以无条件
        清掉标记会在另一条级联还在拆这棵树的时候把它整个放开——那正是本级联存在的理由被绕过的
        样子（理由见模块文档）。

        没有对应钉住的 id 记一条 ERROR 而不是抛：本方法跑在 `finally` 里，一个 KeyError 会同时
        吃掉在飞的异常、并让这一批里剩下的 id 一份都撤不掉，那些位置于是永久挂不上新子代。
        """
        for agent_id in agent_ids:
            holders = self._doomed.get(agent_id)
            if holders is None:
                logger.error(
                    '期望: agent %s 的钉住标记还在，可以撤掉本次级联放上去的那一份。'
                    ' 实际: 表里没有它的标记——钉住与撤销失去了配平，'
                    '本次放上去的那一份已经被别处撤掉了。'
                    ' 下一步: 用 find() 确认这棵子树的末态；'
                    '此后往这些位置 spawn 子代会被误拒或误放行，重建这棵树最省事。',
                    agent_id,
                )
                continue
            if holders > 1:
                self._doomed[agent_id] = holders - 1
            else:
                del self._doomed[agent_id]

    async def _destroy(self, agent_id: str) -> None:
        """拆掉一个 Agent 的全部痕迹。幂等，且 `AGENT_DESPAWN` 对一次消失恰好报一次。

        本进程没有它的实例时只摘条目与队列：树的形状与本进程持有哪些实例无关。

        **别的级联正在拆同一个条目时，这里照样自己动手。** 不等它：那是一条落在**平级调用方**
        身上的阻塞边，可以成环（理由见模块文档）。也不因为它在拆就早退：那是把自己的后置条件转交
        给另一个执行体，而它被取消时没有人兑现。摘是幂等的，报不是——报由收据定，见 `_Teardown`。
        """
        teardown = self._destroying.get(agent_id)
        if teardown is None:
            teardown = _Teardown()
            self._destroying[agent_id] = teardown
        teardown.holders += 1
        try:
            if not await self._has_traces(agent_id):
                return
            task = self._table.get_task(agent_id)
            # 读 `_DRIVING_AGENT` 而不是比对任务身份：`ExitMode.EXIT` 是在驱动任务自己的 finally
            # 里走到这条路的，取消并 await 自己就是永久挂死，而可并发的工具跑在 `gather` 的子任务
            # 里、按任务身份认不出这一格。等一个子代的驱动任务停下来是往自己子树里等，不成环。
            if task is not None and _DRIVING_AGENT.get() != agent_id:
                task.cancel()
                await asyncio.wait({task})
            await self.broker.remove_queue(agent_id)
            await self.registry.unregister(agent_id)
            self._table.pop(agent_id)
            self._slots.pop(agent_id, None)
            if self._workspace is not None:
                await self._workspace.release(agent_id)
            if await self._has_traces(agent_id):
                # 复检而不是相信 `unregister` 的明文契约：这是本方法唯一对外承诺的东西，而剩下
                # 痕迹时没有第二个执行体会来兑现它。它没消失，所以既不报 `AGENT_DESPAWN`（那会是
                # 假事件），也不能一声不响返回——这条记录是这次丢失留下的全部痕迹。
                logger.error(
                    '期望: agent %s 的痕迹在拆除之后全部消失——'
                    'unregister 返回即意味着条目已被摘掉。'
                    ' 实际: 摘完之后它还在（注册表条目、本进程的 slot 或实例三者之一），'
                    '说明这一步没有真的生效；它的父即将被注销，'
                    '它的 parent_id 从此指向一个不存在的条目，children_of 再也找不到它。'
                    ' 下一步: 用 find() 确认它还在，再按 agent_id 单独 despawn 它；'
                    '它的父已经不在，只能这样够得着它。反复如此就换一个注册表实现——'
                    '这个实现没有兑现 unregister 的契约。',
                    agent_id,
                )
                return
            self._tracing.forget_receiver(agent_id)
            # 收据是同步取的，因此两条并发的拆除里只有一个取得到；取到之前拆除的每一步都已经跑完
            # 并复检过，所以这个切点报的仍是既成事实。
            if not teardown.reported:
                teardown.reported = True
                self._tracing.event('agent.despawned', {'agent.id': agent_id})
                # 切点在拆除自己的出处里、在任何 span 之外跑：取得到收据的可以是几条并发拆除中的
                # 任何一条（despawn、级联、EXIT 收尾、kill 的收尾），hook 派的活按调用方取键、记在
                # 调用方的 span 上，就随谁先到而变（M3 D10 / D3）。一个 Agent 至多消失一次，计数
                # 因此不必留着。
                owner = entering_owner(('despawn', agent_id), {})
                spans = outside_any_span()
                try:
                    await self.hooks._dispatch(Junction.AGENT_DESPAWN, agent_id=agent_id)  # pyright: ignore[reportPrivateUsage]
                finally:
                    back_in_spans(spans)
                    leaving_owner(owner)
        finally:
            # 只摘自己那一份：清掉整张表会撤掉别的在飞拆除共享的那份收据，于是同一次消失会被
            # 第二条级联再报一次。
            teardown.holders -= 1
            if teardown.holders <= 0:
                self._destroying.pop(agent_id, None)

    async def _has_traces(self, agent_id: str) -> bool:
        """本进程或注册表里还留着这个 Agent 的东西吗。"""
        return (
            agent_id in self._slots
            or self._table.get(agent_id) is not None
            or await self.registry.get(agent_id) is not None
        )

    async def _reap(self, agent_id: str, slot: _Slot) -> None:
        """在事件循环之外收尾。

        `_drive` 的 finally 在取消路径上刻意不收尾，因此**被取消的那一次退出只有这里保证得了归宿**
        ——少了它，Agent 停在 ABORTING，而 `ensure_active` 明文拒绝复活一个正在收尾的 Agent。

        收尾要写注册表、摘队列，而这些 await 落在一个正在被取消的任务里会再次被取消——内存实现没有
        挂起点所以看不出来，远端实现下就是收了一半的尾。放进独立任务，`kill` 因此也能立刻返回。

        异常在这里收口：收割任务没有调用方，逃出去的异常无人取回，只会在 GC 时变成一行看不出主体
        的日志。
        """
        # 本任务由 `kill` 建，可能建在某个 Agent 的驱动任务里，于是继承了它的 `_DRIVING_AGENT`。
        # 收尾刻意跑在事件循环之外，那个值在这里是假的：留着它，一次落在那个 Agent 身上的拆除会被
        # 误判成「等自己」而拒掉，被杀的 Agent 于是停在 aborting、再也复活不了。
        _DRIVING_AGENT.set(None)
        # 同理它也继承了发起 kill 的那次工具调用与当前 span：不清掉，收尾里取的键会与那次调用抢序号
        # （谁先谁后取决于调度，M3 D10），事件与边记到那次早已结束的调用上。收尾自己不派活：拆除的
        # hook 在 `_destroy` 里换到它自己的出处。
        becoming_owner(('reap', agent_id), {})
        outside_any_span()
        no_installer()
        try:
            task = self._table.get_task(agent_id)
            if task is not None:
                await asyncio.wait({task})
            await self._finish(agent_id, slot, task)
        except Exception:
            logger.exception(
                '期望: agent %s 被取消之后的收尾走完，它落到 exit_mode 说的那个归宿上。'
                ' 实际: 收尾自己抛了异常，它可能停在 aborting——而 aborting 的 Agent 复活不了。'
                ' 下一步: 用 find() 读它当前的状态；停在 aborting 就 despawn 它再重新 spawn。',
                agent_id,
            )
        finally:
            self._reaping.discard(agent_id)

    def _set_tracer(self, tracer: Tracer) -> None:
        """换上接收插桩的 tracer（内部入口：tracer 的公开面还没定）。已经在跑的 Agent 从下一次
        插桩起记到它：它们持有的是同一份 `_Tracing`。

        还有 span 开着时拒绝：开始已经交给了旧的，结束会落到新的上，两边都配不上对。旧 tracer 的
        失败在换掉之前报掉，失败计数清零——总数报给谁就得是谁的。
        """
        _checked_tracer(tracer)
        if self._tracing.open_spans:
            raise ConfigError(
                expected='换 tracer 时没有开着的 span',
                received=f'还有 {self._tracing.open_spans} 个 span 开着（有 Agent 正在跑）',
                next_action='在 spawn 任何 Agent 之前换，或者等它们都停下（wait / shutdown）再换',
            )
        self._report_tracer_failures()
        self._tracing.failures = 0
        self._tracing.tracer = tracer

    def _report_tracer_failures(self) -> None:
        count = self._tracing.failures
        if not count:
            return
        logger.error(
            '期望: tracer %s 收下这个 Runtime 的每一次插桩。'
            ' 实际: 它一共失败了 %d 次，这些记录都丢了（第一次的异常在当时那条日志里）。'
            ' 下一步: 修好这个 tracer 再跑一次；这次运行的轨迹不完整，不要拿它回放或导出。',
            type(self._tracing.tracer).__name__,
            count,
        )

    def _refuse_self(self, operation: str, agent_id: str) -> None:
        """拒绝一个要等目标停下、而目标就是调用者自己的操作。

        这不是「冲突」而是意图无法表达，所以遍历找到的主体上照样抛：等自己就是永久挂死。

        判据是 `_DRIVING_AGENT` 而不是 `asyncio.current_task()`：可并发的工具跑在 `gather` 建的
        子任务里，而那个驱动任务正阻塞在它上面——按任务身份比对会静默放行，换来的正是这里要挡的
        那次挂死。
        """
        if _DRIVING_AGENT.get() != agent_id:
            return
        raise ContractViolation(
            expected=f'{operation} 的目标子树里不含发起这次调用的那个 Agent 自己',
            received=f'这次 {operation} 落在 agent {agent_id!r} 自己的事件循环里',
            next_action=(
                f'{operation} 要等目标的事件循环停下来，等自己就是永久挂死。'
                '要让自己停下来用 abort（它在下一轮边界上兑现，轨迹保持一致）；'
                '要让别人把你销毁，就把这件事交给你的父 Agent'
            ),
        )

    async def _abort_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('abort', agent_id, named=named)
        if slot is None or info is None:
            return
        # 两个读都是同步的，且此后到 `request_abort()` 为止只隔着一次 CAS：驱动任务在本函数让出
        # 控制权之前动不了，因此「它此刻没有在飞的轮」这个判断不会被一个并发开始的周期推翻。
        if not self._has_live_driver(agent_id) or slot.parked_on_mailbox:
            return
        # 先落注册表再落位：记不下 ABORTING 就不要真的置位——那会停掉一件谁都看不出被停了的活。
        # 反过来这两句之间也没有挂起点，所以驱动任务看到的永远是「状态与位一起到」。
        if not await self._switch(agent_id, new=AgentStatus.ABORTING, allowed=_LIVE_STATUSES):
            self._refuse_control(
                'abort',
                agent_id,
                named=named,
                received='转到 aborting 的 CAS 被拒，它的状态刚刚被别人改过',
                next_action='用 find() 重读它的状态再决定；不要在读到的旧状态上重试',
            )
            return
        slot.control.request_abort()
        # 中止位在轮开始前和第 9 步被读到，而此刻这个 Agent 可能卡在一轮中间的一次等待里：不打断
        # 它，中止要等那次等待自己结束才兑现——等答复那条可以是几小时的超时，等子 Agent 干活那条
        # 根本没有上限。两个槽都要打：它们是两种在轮等待，各有自己的令牌。
        for interrupted in (slot.waiter, slot.delegation):
            if interrupted is not None:
                interrupted.interrupt(WaitState.ABORTED)

    async def _pause_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('pause', agent_id, named=named)
        if slot is None or info is None:
            return
        # 已经是 PAUSED 的一并放行：后置条件已经成立的操作成功返回。
        if info.status not in (*_PAUSABLE_STATUSES, AgentStatus.PAUSED):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received=f'它当前是 {info.status.value}',
                next_action=(
                    f'只有 {" / ".join(status.value for status in _PAUSABLE_STATUSES)} 能挂起。'
                    'zombie 没有事件循环可停，而它的注册表状态正是复活它的凭据；'
                    'aborting 正在收尾。要让它彻底不再被叫醒就用 despawn'
                ),
            )
            return
        # 先落注册表再落门：记不下 PAUSED 就不要真的停——那会停在一个谁都看不见的地方。
        if not await self._switch(agent_id, new=AgentStatus.PAUSED, allowed=_PAUSABLE_STATUSES):
            self._refuse_control(
                'pause',
                agent_id,
                named=named,
                received='转到 paused 的 CAS 被拒，它的状态刚刚被别人改过',
                next_action='用 find() 重读它的状态再决定；不要在读到的旧状态上重试',
            )
            return
        slot.control.pause()

    async def _resume_one(self, agent_id: str, *, named: bool) -> None:
        slot, info = await self._control_target('resume', agent_id, named=named)
        if slot is None or info is None:
            return
        slot.control.resume()
        back_to = _resumed_status(slot)
        if not await self._switch(agent_id, new=back_to, allowed=(AgentStatus.PAUSED,)):
            # WARNING 而不是 INFO：root logger 默认停在 WARNING，INFO 的记录根本不会被创建，而
            # 本模块的不变式是「每一处状态转换失败都必须留痕」。也不是 ERROR：门已经抬起、这个
            # Agent 照常跑，任何远端注册表上「它自己先往下走了」都会让 ERROR 变成常态误报。
            logger.warning(
                '期望: agent %s 在注册表里是 paused，可由本进程转回 %s。'
                ' 实际: 这次 CAS 被拒，它此刻不是 paused——'
                '它可能已经自己往下走了，也可能状态归了别人。'
                ' 下一步: 门已经抬起，这个 Agent 可以继续跑；'
                '用 find() 重读它的状态，不要在读到的旧状态上重试。',
                agent_id,
                back_to.value,
            )

    async def _control_target(
        self, operation: str, agent_id: str, *, named: bool
    ) -> tuple[_Slot | None, AgentInfo | None]:
        """取出控制面操作的目标；取不到时按 named 决定抛还是记日志。

        **驱动任务没有接管过它，就不算取到。** 控制面写的是共享注册表里的状态，而这里的门只按得住
        本进程的事件循环；接管失败的那个条目归别人所有，替它写下 PAUSED 之后谁的 `ensure_active`
        都转不回 IDLE（`allowed` 只有 ZOMBIE），而 `send` 仍旧照答 ok=True——一张通往没有消费者的
        邮箱的收据。
        """
        slot = self._slots.get(agent_id)
        info = await self.registry.get(agent_id)
        if slot is None or info is None:
            self._refuse_control(
                operation,
                agent_id,
                named=named,
                received=(
                    '注册表里没有这个条目'
                    if info is None
                    else '本进程没有它的实例——它由别的进程持有'
                ),
                next_action=(
                    '确认 agent_id 是 spawn 的返回值；'
                    '被 despawn 或以 ExitMode.EXIT 结束的 Agent 已被销毁，只能重新 spawn。'
                    '若它由其他进程持有，在那个进程上操作它'
                ),
            )
            return None, None
        if not slot.owned:
            self._refuse_control(
                operation,
                agent_id,
                named=named,
                received=(
                    '本进程 spawn 过它，但驱动任务还没有接管：'
                    'spawn 只是把任务排上去，它可能还没被调度到；'
                    '也可能 IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                    '——本进程没有它的事件循环可以停'
                ),
                next_action=(
                    '刚 spawn 完就调用的话先重试一次：每一次 await 都给驱动任务一个接管的机会。'
                    '重试仍是这一条就用 find() 读它当前的 status 与 owner，在持有它的进程上操作它；'
                    '确认它空闲后重新 spawn 一个，才能在本进程里控制它'
                ),
            )
            return None, None
        return slot, info

    def _refuse_control(
        self, operation: str, agent_id: str, *, named: bool, received: str, next_action: str
    ) -> None:
        """点名的目标抛异常，遍历到的子代只记日志。"""
        if not named:
            logger.info(
                '%s 跳过子代 %s：%s。这是尽力而为的遍历，不因此中断。',
                operation,
                agent_id,
                received,
            )
            return
        raise ContractViolation(
            expected=f'agent {agent_id!r} 处在可以 {operation} 的状态',
            received=received,
            next_action=next_action,
        )

    async def _descendants(self, agent_id: str) -> list[str]:
        """广度优先列出全部子代，浅的在前。

        `visited` 不是防御性编程：`children_of` 读的是注册表，而注册表可以是远端的、可以被别的
        进程写过；一个环会让这个遍历永不返回，而它是 pause / abort / despawn 三条路的公共前置。
        """
        found: list[str] = []
        visited = {agent_id}
        frontier = [agent_id]
        while frontier:
            following: list[str] = []
            for parent in frontier:
                for child in await self.registry.children_of(parent):
                    if child in visited:
                        continue
                    visited.add(child)
                    found.append(child)
                    following.append(child)
            frontier = following
        return found

    async def wait_for(self, agent_id: str, waiter: Waiter) -> WaitResult:
        """代表一个正在干活的 Agent 等一条匹配的答复（spec §4.2，供工具使用）。

        **它在轮里面等，不在事件循环里等。** 一次 `Confirmation` 等待从工具调用内部发起，所以此刻
        这个 Agent 正在一轮的中间：turn 循环挂在这里，注册表说 `WAITING`。它的邮箱仍由门在下一轮
        开始前消费，两件事各等各的——被等到的那条答复由 `take_by_correlation` 从邮箱里取走，因此
        不会再作为普通消息注入一遍。没人在等的答复就是一条普通消息，照常在下一轮进上下文。

        **一个 Agent 同时只有一个等待者，新的顶掉旧的，旧的收到 `preempted`。** 一个 Agent 是一个
        进程、一份注意力；两个并发的等待里必然有一个的答复没人会来。让旧的挂到超时是最坏的选择
        ——发起它的那次工具调用会一直占着位置，而「一次调用恰好一个结果块」这条不变式要求它必须能
        返回点什么。被拒的等待**不**顶掉在飞的那个：拒绝意味着什么都没发生。

        **答复优先于中断**：两者同时就绪时返回 `answered`。消息已经离开邮箱了，此刻丢掉它就是真的
        丢了。

        **关联 ID 是一个扁平命名空间，本方法不给它加前缀。** `Confirmation` 等的是模型厂商生成的
        `tool_call_id`，`Reply` 等的是 `send(correlation_id=...)` 写进消息的那个串，两者共用同一个
        字符串空间，因此一条对派活的回复原则上能解掉一次工具确认的等待。加前缀要求发答复的那一侧
        用同一套编码，而两侧的编码只要有一处不一致，症状就是答复躺在邮箱里、等待跑满超时、零日志
        ——比当前的碰撞风险更难查。

        等待先取 `Waiter.timeout`，再由 `SafetyModule.max_wait_seconds` 做宿主级 clamp；Flag 为 0
        时原样交给 broker，不凭空截短一次合法的长等待。

        `waiter` 不是三种登记类型之一（或同时是两种）抛 `ContractViolation`；它的关联 ID 本身不合法
        抛 `ConfigError`。两者都在**改动状态之前**抛出，被拒的调用因此不会把这个 Agent 留在
        `WAITING`。

        **装上令牌之后复检中止位**：转 WAITING 的那次 CAS 可以在生效之后才返回，而那段窗口里的一次
        `abort` 看不到还没装上的令牌。

        **拒绝只留给「本进程真的答不出这个问题」**：没有实例、驱动任务没接管、手上没有一轮活可以
        挂起（idle / zombie / 条目已不在）。控制面留下的两个状态各有归宿而**不是**拒绝——
        `ContractViolation` 从工具里原样上抛会把整个 run 打成 `error`，`abort` 承诺的 `ABORTED`
        与 `pause` 之后的 `resume` 都因此丢掉：

        - `aborting`：直接交回 `WaitState.ABORTED`，**不写注册表**。后置条件本来就是这个，而这一轮
          已经以中止收场，此刻开始一次等待也解不开它。判据取注册表状态而非本地那一位：`kill` 也
          写下 `aborting`（它不武装中止位），收尾跑完之前发起的等待同样走这一支。调用方因此不得把
          `ABORTED` 读成「等过一段时间」——这一支一秒都没等，`waiter.timeout` 也没有交给 broker。
        - `paused`：照常等，但**不写注册表**。轮已经被挂起门按住了，而 `resume` 的 CAS 只收
          `PAUSED`——写下 `WAITING` 会让它失败，用户再也 resume 不回来。挂起请求仍在控制块里，因此
          这一轮照旧在下一个轮边界上停下。

        只作用于本进程接管过的 Agent：替一个别人持有的条目写下 WAITING 之后，谁的 `ensure_active`
        都转不回 IDLE（它的 `allowed` 只有 ZOMBIE），而 `send` 仍旧照答 ok=True。

        从工具调用里（或一个 Agent 自己的事件循环里）发起的等待登记它的稳定键（M3 D10）：等待期间
        发给这个 Agent、带着这个关联 ID 的第一条消息按它取消息 id。键在入口就占用，被拒的等待也
        占掉它的序号；登记在任何 await 之前——会挂起的注册表下，转 WAITING 的那次 CAS 期间就可能
        有答复到达（回放按事件里的键当场作答）。发出请求事件与调用本方法之间不得有挂起点：那段
        窗口里到达的答复认不出它答的是哪个请求。

        每次等待一个 wait span（M3 D3），结构键就是这个请求键：它按出处编号，同一个 Agent 并发的
        几次调用各等各的，编号不随调度变。人等的对象记成请求键，等 `Reply` 的记成关联 ID。
        """
        key = next_key('request')
        if not isinstance(cast(object, agent_id), str) or agent_id not in self._slots:
            # 不记 span：拒绝由 `_wait_for` 给出，它说得清期望什么；调用方给的一个不认识的 id 也
            # 不该原样成为 trace 里一个 span 的 agent id。
            return await self._registered_wait(agent_id, waiter, key)
        if isinstance(waiter, Reply):
            correlation = cast(object, waiter.correlation_id)
            kind, target = 'agent', correlation if isinstance(correlation, str) else ''
        else:
            kind, target = 'human', '' if key is None else json.dumps(list(key))
        with self._wait_span(agent_id, key, kind, target) as span:
            result = await self._registered_wait(agent_id, waiter, key)
            span.attributes['wait.outcome'] = result.state.value
            if result.message is not None:
                self._tracing.wait_answer(span, result.message)
            return result

    def _wait_span(self, agent_id: str, key: StableKey | None, kind: str, target: str) -> _Span:
        if key is None:
            self._host_waits += 1
            key = ('host', 'wait', self._host_waits - 1)
        return self._tracing.span(
            SpanKind.WAIT,
            key,
            name=f'wait {kind}',
            agent_id=agent_id,
            attributes={'wait.kind': kind, 'wait.target': target},
        )

    async def _registered_wait(
        self, agent_id: str, waiter: Waiter, key: StableKey | None
    ) -> WaitResult:
        if key is None:
            return await self._wait_for(agent_id, waiter)
        pending = (agent_id, correlation_of(waiter))
        self._request_keys.setdefault(pending, []).append(key)
        try:
            return await self._wait_for(agent_id, waiter)
        finally:
            waiting = self._request_keys.get(pending)
            # 只摘自己那一个：答复已经取走它时什么都不做，别的等待的登记原样留着。
            if waiting is not None and key in waiting:
                waiting.remove(key)
                if not waiting:
                    del self._request_keys[pending]

    async def _wait_for(self, agent_id: str, waiter: Waiter) -> WaitResult:
        slot = self._slots.get(agent_id)
        if slot is None:
            raise _cannot_wait_for(agent_id, '本进程没有它的实例——它由别的进程持有，或它已被销毁')
        if not slot.owned:
            raise _cannot_wait_for(
                agent_id,
                '本进程 spawn 过它，但驱动任务没有接管：'
                'IDLE -> RUNNING 的转换被拒（它已被别人拿走），或任务在这之前就被取消'
                '——本进程手上没有一轮可以挂起',
            )
        # 校验在改状态之前：一个非法的 Waiter 换来的应当是一次什么都没发生的拒绝，而不是一个停在
        # WAITING、再没有任何答复能解开的 Agent。
        correlation = correlation_of(waiter)
        if not await self._switch(
            agent_id, new=AgentStatus.WAITING, allowed=(AgentStatus.RUNNING,)
        ):
            info = await self.registry.get(agent_id)
            status = None if info is None else info.status
            if status is AgentStatus.ABORTING:
                return WaitResult(state=WaitState.ABORTED)
            if status is not AgentStatus.PAUSED:
                raise _not_running_to_wait(agent_id, info)
        token = _WaitToken()
        previous = slot.waiter
        slot.waiter = token
        if previous is not None:
            previous.interrupt(WaitState.PREEMPTED)
        if slot.control.abort_requested:
            # 上面那次 CAS 可以在**生效之后**才返回（远端注册表就是这样），而那段窗口里的一次 abort
            # 看不到还没装上的令牌，于是这次等待会一直挂到自己超时——中止的整个意义就没了。复检的是
            # 中止位本身而不是一个信号，且**只读不取**：位由轮边界的 `hold_until_runnable` 消费，在
            # 这里取走它，这一轮就不会以 ABORTED 收场了。
            token.interrupt(WaitState.ABORTED)
        cancelled = False
        try:
            if token.raised.done():
                # 令牌已经被打过了：向 broker 发一次注定要被取消的请求只是多一次往返。这条路上
                # 匹配的答复留在邮箱里退回成一条普通消息，与被中断的等待是同一个归宿。
                return WaitResult(state=token.raised.result())
            ceiling = _wait_ceiling(slot.flags)
            timeout = waiter.timeout if ceiling is None else min(waiter.timeout, ceiling)
            return await self._awaited(agent_id, correlation, timeout, token)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # `is token` 两处都要：被顶掉的那个等待先返回时，槽里装的是顶掉它的那个令牌，而那次
            # 等待还挂着——此刻恢复 RUNNING 会让注册表宣布一个正悬在等待里的 Agent 在跑。
            if slot.waiter is token:
                slot.waiter = None
                if not cancelled:
                    # 回不去 RUNNING 是正常的：abort 已经把它转成 ABORTING、pause 转成了 PAUSED，
                    # 强行覆盖等于抹掉别人的转换。取消路径上一律不写注册表——归宿由发起取消的那一方
                    # 在取消之外给出（见 `_reap`），而这个 await 落在一个正在被取消的任务里，远端
                    # 注册表下就是写了一半。
                    await self._switch(
                        agent_id, new=AgentStatus.RUNNING, allowed=(AgentStatus.WAITING,)
                    )

    async def _awaited(
        self,
        agent_id: str,
        correlation: str,
        timeout: float,  # noqa: ASYNC109
        token: _WaitToken,
    ) -> WaitResult:
        """等到答复、中断或超时，三者取先到的那一个。

        取消一次在飞的 `take_by_correlation` 必须**当场生效**，且**不得吃掉已经取出的那条消息**
        （消息要留在 `consume` 看得见的地方）——两条都是 `MessageBroker` 的明文契约并由契约套件
        强制。少了后者，被中断的等待会静默销毁一条答复。

        **下面那条收回 `taking` 的等待边，长度由 broker 决定。** 它沿着自己 `create_task` 出来的
        后代走，因此不可能成环；但取消不当场生效时它是**无界**的——`abort` / `shutdown` /
        `despawn` 全都排在它后面，于是「不成环」并不等于「有界」。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        taking = asyncio.create_task(
            self.broker.take_by_correlation(agent_id, correlation, timeout),
            name=f'wait:{agent_id}:{correlation}',
        )
        try:
            await asyncio.wait({taking, token.raised}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 只取消 taking：`token.raised` 归令牌所有，取消它会让此后的 `interrupt` 静默落空。
            taking.cancel()
            await asyncio.wait({taking})
        # 无论谁胜出都读一次异常。读它是取回的动作本身：不读就是把 broker 抛出的东西留在一个无人
        # 认领的任务里，只在 GC 时变成一行看不出主体的日志。
        failure = None if taking.cancelled() else taking.exception()
        answer = None if taking.cancelled() or failure is not None else taking.result()
        if answer is not None:
            # 答复优先于中断：消息已经离开邮箱了，此刻丢掉就是真的丢了。
            self._tracing.consumed(answer.id)
            return WaitResult(state=WaitState.ANSWERED, message=answer)
        if token.raised.done():
            state = token.raised.result()
            if failure is not None:
                self._report_discarded_take_failure(agent_id, correlation, state, failure)
            return WaitResult(state=state)
        if failure is not None:
            raise failure
        # 只在真的交回 timeout 的那条路上报告。中断胜出时交回的是中断状态，此处报一条说「本次
        # 交回 timeout」的 ERROR 就是让唯一的那份证据说谎。
        self._report_unwaited_timeout(agent_id, correlation, timeout, deadline - loop.time())
        return WaitResult(state=WaitState.TIMEOUT)

    def _report_discarded_take_failure(
        self, agent_id: str, correlation: str, state: WaitState, failure: BaseException
    ) -> None:
        """中断先到，而在飞的那次取消息抛了异常——这条记录是那次故障留下的全部痕迹。

        交回的 `state` 说的是「这次等待被打断了」，broker 坏掉这件事在四态里表示不出来，而这个
        异常此后再没有调用方。丢掉它就是把一次真实故障静默丢掉，然后下一次等待撞上同一个故障。
        """
        logger.error(
            '期望: agent %s 上按 %r 取消息的那次调用，要么交回一条答复，要么被取消后安静退出。'
            ' 实际: 它抛了 %s: %s，而这次等待已经先以 %s 收场——'
            '这个异常此后没有调用方，本条记录是它留下的全部痕迹。'
            ' 下一步: 按这个异常修 broker（连接、鉴权、序列化都走这条路）；'
            '在同一个关联 ID 上重试等待之前先确认它已经能取到消息，否则下一次撞上同一个故障。',
            agent_id,
            correlation,
            type(failure).__name__,
            failure,
            state.value,
        )

    def _report_unwaited_timeout(
        self, agent_id: str, correlation: str, timeout: float, remaining: float
    ) -> None:
        """`take_by_correlation` 在截止时刻之前就答了「没有」——它只在邮箱不在时这么答。

        `WaitState` 由 spec §4.2 定死四态，而这一支与超时的下一步是同一件事（回去重读这个 Agent 的
        状态），所以它并入 `timeout`。**区别只剩这条记录**：交回的 `timeout` 说的是「没等到」，而
        调用方会读成「等满了那么久」，一次零秒的等待于是看着像一次几十秒的等待。
        """
        if remaining <= 0:
            return
        logger.error(
            '期望: agent %s 上等 %r 的这次等待，要么等到一条匹配的答复，要么等满 %.3fs 的超时。'
            ' 实际: broker 在还剩 %.3fs 时就答了「没有匹配项」，它只在邮箱不存在时这样答——'
            '邮箱已被 remove_queue 摘掉或从未建起来，此后不会有任何答复到达；'
            '本次交回 timeout，但它并没有等满。'
            ' 下一步: 用 find() 读这个 agent 当前的状态；'
            '不要在同一个关联 ID 上重试等待——邮箱没了，重试还是这个答案。'
            '条目还在就先 ensure_active 把邮箱建回来，再重发那条请求。',
            agent_id,
            correlation,
            timeout,
            remaining,
        )

    async def assemble(
        self,
        topology: Topology,
        catalog: AgentCatalog | Mapping[str, AgentFactory],
        *,
        tools: Mapping[str, Tool],
        models: Mapping[str, LLM] | None = None,
        host: object = None,
        danger_judge: DangerJudge | None = None,
        danger_policy: DangerPolicy | None = None,
        role_policies: Mapping[str, DangerPolicy] | None = None,
        artifact_kinds: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """把拓扑装进本 Runtime。这不是 `start`，也不发 Runtime 启动切点。

        只 spawn `boot='eager'` 的 singleton。lazy 等 `send_to_slot`，manual 等 `occupy`。
        第二次调用是契约违反：要换形态就新 Runtime。中途 spawn 失败也已经算装过，避免重试把
        已经起来的 eager 再 spawn 一遍。

        第二个参数是目录，或已经冻结好的工厂（`{角色名: 工厂}`）。交目录时照旧冻结：`models`
        必填，`danger_judge` / `danger_policy` / `role_policies` 原样交给 `AgentCatalog.freeze`，
        槽上建出的与 `Delegate` 按目录角色派出的 Agent 因此都受约束。交冻结工厂时不再冻结一次
        （回放与场景用它：工厂里已经有模型与处置，再给就是两份可能不一致的配置），每个角色的
        `artifact_kinds` 由调用方给出。两种入参走同一段装配逻辑：绑定、别名、`_SLOT_SPAWN`、
        实例 metadata。`Runtime(agents=)` 里同名的工厂被替换，别的名字不受影响。
        """
        from syndica.kernel.catalog import AgentCatalog
        from syndica.kernel.topology import Topology

        if self._closed:
            raise _already_shut_down()
        # 签名挡不住绕过类型检查的调用方，所以先收成 object 再判真实类型。
        received_topology = cast(object, topology)
        if not isinstance(received_topology, Topology):
            raise ConfigError(
                expected='topology 是 Topology',
                received=f'收到 {type(received_topology).__name__} 类型的 {received_topology!r}',
                next_action='传入 Topology(slots=...)；不要传裸列表',
            )
        received_catalog = cast(object, catalog)
        if not isinstance(received_catalog, AgentCatalog | Mapping):
            raise ConfigError(
                expected='catalog 是 AgentCatalog，或「角色名 → 已冻结的工厂」的映射',
                received=f'收到 {type(received_catalog).__name__} 类型的 {received_catalog!r}',
                next_action='传入 AgentCatalog；先 load_mapping / register_class 再 assemble',
            )
        if self._assembled:
            raise ContractViolation(
                expected='一个 Runtime 只 assemble 一次',
                received='assemble 已被调用过',
                next_action='要换拓扑就新建一个 Runtime；不要在同一个进程上重装',
            )
        received_tools = cast(object, tools)
        if isinstance(received_tools, (str, bytes)) or not isinstance(received_tools, Mapping):
            raise ConfigError(
                expected='tools 是「名字 → Tool」的映射',
                received=f'收到 {type(received_tools).__name__} 类型的 {received_tools!r}',
                next_action='传入 {"Read": read_tool} 这样的表。槽上的 extra_tools 按这张表解析',
            )
        tool_table = cast('Mapping[str, Tool]', received_tools)
        received_topology.validate(cast(object, received_catalog))
        if isinstance(received_catalog, AgentCatalog):
            if artifact_kinds is not None:
                raise ConfigError(
                    expected='交目录时不给 artifact_kinds：每个角色的清单里已经写着',
                    received='同时收到 catalog 与 artifact_kinds',
                    next_action='删掉 artifact_kinds；只有交冻结工厂时才由调用方给出',
                )
            if models is None:
                raise ConfigError(
                    expected='交目录时给 models：冻结角色要按清单里的模型名取到 LLM',
                    received='models=None',
                    next_action='传 models={模型名: LLM}，或改交已经冻结好的工厂',
                )
            frozen = received_catalog.freeze(
                tool_table,
                models,
                host,
                danger_judge=danger_judge,
                danger_policy=danger_policy,
                role_policies=role_policies,
            )
            kinds = {name: tuple(received_catalog.get(name).artifact_kinds) for name in frozen}
        else:
            given = {
                'models': models,
                'host': host,
                'danger_judge': danger_judge,
                'danger_policy': danger_policy,
                'role_policies': role_policies,
            }
            extra = sorted(name for name, value in given.items() if value is not None)
            if extra:
                raise ConfigError(
                    expected='交冻结工厂时不再给冻结用的参数：工厂里已经有模型与处置',
                    received=f'同时收到 {extra}',
                    next_action='删掉这些参数，或改交目录让 assemble 去冻结',
                )
            frozen = dict(cast('Mapping[str, AgentFactory]', received_catalog))
            validate_agent_factories(frozen)
            kinds = _artifact_kinds(artifact_kinds, frozen)
        for name, factory in frozen.items():
            if name in self._agents:
                logger.info('catalog 覆盖了 Runtime(agents=) 里同名工厂 %s', name)
            self._agents[name] = factory
            self._agent_installers[name] = current_installer()
        self._topology_tools = dict(tool_table)
        bindings: dict[str, _TopologyBinding] = {}
        for slot in received_topology.slots:
            bindings[slot.name] = _TopologyBinding(
                slot=slot, artifact_kinds=kinds.get(slot.agent, ())
            )
        self._topology_bindings = bindings
        self._topology_aliases = {}
        self._topology_alias_owners = {}
        self._assembled = True
        for slot in received_topology.slots:
            if slot.boot == 'eager':
                await self._spawn_slot(bindings[slot.name], key=None)

    async def resolve(
        self,
        name: str,
        *,
        key: str | None = None,
        spawn: bool = False,
    ) -> str | None:
        """按槽名取已有别名。`spawn=False` 不创建。on-demand 永远 None。

        manual 即使 `spawn=True` 也不创建：没有调用方的显式 occupy，它不该出现。
        """
        binding = self._binding(name)
        slot = binding.slot
        alias = _alias_for(slot, key)
        if alias is None:
            return None
        existing = self._owned_alias(slot, alias)
        if existing is not None:
            return existing
        if not spawn:
            return None
        if slot.boot == 'manual':
            raise ConfigError(
                expected=f'槽 {name!r} 是 manual，resolve 不会创建它',
                received='spawn=True',
                next_action='用 occupy 显式创建；manual 不会因寻址而出现',
            )
        return await self._spawn_slot(binding, key=key)

    async def occupy(self, name: str, *, key: str | None = None) -> str:
        """显式要一个实例。singleton / 同一 key 再次调用返回同一个 agent_id。

        on-demand 每次都新建，并且不进别名表。
        """
        binding = self._binding(name)
        alias = _alias_for(binding.slot, key)
        if alias is not None:
            existing = self._owned_alias(binding.slot, alias)
            if existing is not None:
                return existing
        return await self._spawn_slot(binding, key=key)

    async def send_to_slot(
        self,
        name: str,
        content: str,
        *,
        key: str | None = None,
        trigger: Trigger = Trigger.PENDING,
        activate: bool = True,
        sender: str = 'runtime',
        correlation_id: str | None = None,
        trust: TrustLevel = TrustLevel.TRUSTED,
    ) -> DeliveryResult:
        """按槽名投递。lazy 的第一次寻址会创建；manual 与 on-demand 拒绝。

        不把槽名塞进 `send` 的第一个参数：那一位是 agent_id。两条寻址混在一个参数里，
        调用方无法从类型上看出自己传的是哪一种。
        """
        binding = self._binding(name)
        slot = binding.slot
        if slot.cardinality == 'on-demand' or slot.boot == 'manual':
            raise ConfigError(
                expected=('send_to_slot 只服务 singleton / keyed；lazy 会在第一次寻址时创建'),
                received=f'槽 {name!r} cardinality={slot.cardinality!r} boot={slot.boot!r}',
                next_action='这种槽用 occupy 拿到 agent_id，再 send(agent_id, ...)',
            )
        agent_id = await self.resolve(name, key=key, spawn=True)
        if agent_id is None:
            raise ContractViolation(
                expected=f'槽 {name!r} 在 send_to_slot 时已经有或刚创建出 agent_id',
                received='resolve 返回 None',
                next_action='on-demand 与 manual 走 occupy；这条路径只服务 singleton 与 keyed',
            )
        return await self.send(
            agent_id,
            content,
            trigger=trigger,
            activate=activate,
            sender=sender,
            correlation_id=correlation_id,
            trust=trust,
        )

    def _binding(self, name: str) -> _TopologyBinding:
        if not self._assembled:
            raise ConfigError(
                expected='先 assemble 再按槽名寻址',
                received=f'槽 {name!r}，Runtime 还没有装配拓扑',
                next_action=('先 await runtime.assemble(topology, catalog, tools=..., models=...)'),
            )
        binding = self._topology_bindings.get(name)
        if binding is None:
            known = sorted(self._topology_bindings)
            raise ConfigError(
                expected=f'槽名是已装配拓扑里的一个：{known}',
                received=f'name={name!r}',
                next_action='用 Topology 里的 slot.name，不要用角色名或 agent_id',
            )
        return binding

    async def _spawn_slot(self, binding: _TopologyBinding, *, key: str | None) -> str:
        slot = binding.slot
        base = self._agents.get(slot.agent)
        if base is None:
            raise ConfigError(
                expected=f'角色 {slot.agent!r} 的工厂在 assemble 时已经并进 Runtime',
                received=f'agent_names={list(self._agents)!r}，没有 {slot.agent!r}',
                next_action='确认 assemble 用的 catalog 里有这个角色，并且没有在之后删掉工厂',
            )
        alias = _alias_for(slot, key)
        if alias is not None:
            existing = self._owned_alias(slot, alias)
            if existing is not None:
                return existing
        token = _SLOT_SPAWN.set(
            _SlotSpawn(
                slot.name,
                key or '',
                per_caller=slot.cardinality == 'on-demand',
                installer=self._agent_installers[slot.agent],
            )
        )
        try:
            agent_id = await self.spawn(
                self._enhanced_factory(base, slot),
                exit_mode=slot.exit_mode,
                metadata=self._instance_metadata(slot, binding.artifact_kinds),
            )
        finally:
            _SLOT_SPAWN.reset(token)
        if alias is not None:
            self._topology_aliases[alias] = agent_id
            self._topology_alias_owners[alias] = slot.name
        return agent_id

    def _owned_alias(self, slot: AgentSlot, alias: str) -> str | None:
        """同一槽再次算出这个别名时返回已有 agent_id。别的槽占用了它则拒绝。"""
        existing = self._topology_aliases.get(alias)
        if existing is None:
            return None
        owner = self._topology_alias_owners.get(alias)
        if owner == slot.name:
            return existing
        raise ConfigError(
            expected=f'别名 {alias!r} 还没有被占用',
            received=f'槽 {slot.name!r} 算出的别名已被占用（当前属于槽 {owner!r}）',
            next_action='改槽名。别名是槽名或「槽名:键」，两个槽不得算出同一个',
        )

    def _enhanced_factory(self, base: AgentFactory, slot: AgentSlot) -> AgentFactory:
        """包一层槽增强。工厂闭包里的 AgentOptions 可能被多次调用复用，这里只换本实例的那一份。"""

        def factory(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            agent = base(hooks, events)
            self._apply_slot_enhance(agent, slot)
            return agent

        return factory

    def _apply_slot_enhance(self, agent: BaseAgent, slot: AgentSlot) -> None:
        """解析槽的工具、收紧预算。工具只挂在 Agent 上，由 spawn 在 Module 工具之后装：增强留在
        工厂里，`Fork` 经 `_factory_of` 重新 spawn 时分身才拿到同样的工具与次序。"""
        from syndica.kernel.catalog import resolve_tool_names

        if slot.budget is not None:
            tightened = _tightened_max_turns(
                agent.options.max_turns, slot.budget['turns'], slot.agent
            )
            agent.options = replace(agent.options, max_turns=tightened)
        if slot.extra_tools:
            resolved = resolve_tool_names(slot.extra_tools, self._topology_tools)
            agent._hold_slot_tools(slot.name, slot.agent, resolved)  # pyright: ignore[reportPrivateUsage]

    def _instance_metadata(self, slot: AgentSlot, kinds: tuple[str, ...]) -> dict[str, Any]:
        owned = copy.deepcopy(dict(slot.metadata))
        if 'artifact_kinds' in owned:
            raise ConfigError(
                expected='槽 metadata 不得占用 artifact_kinds',
                received=f'槽 {slot.name!r} 的 metadata 含 artifact_kinds',
                next_action='删掉这个键。产物种类写在角色清单上，occupy 时再并进来',
            )
        owned['artifact_kinds'] = kinds
        return owned

    async def find(self, agent_id: str) -> AgentInfo | None:
        """只读路径，绝不激活、也绝不创建。已有别名会解析成当时的 agent_id。"""
        mapped = self._topology_aliases.get(agent_id)
        return await self.registry.get(mapped if mapped is not None else agent_id)

    async def shutdown(self) -> None:
        """取消所有事件循环，**等它们停下来**再关事件总线。幂等，且是终态：此后 spawn 报错。

        取消不等于停下：任务还要一路展开出来。不等它们停下，未结束的任务会在解释器退出时留下
        一串 "Task was destroyed but it is pending"。
        """
        chain = _SHUTTING_DOWN.get()
        if self in chain:
            # 生命周期回调已在这条清理链里；A 关 B、B 再关 A 也会形成同一种永久环。
            return
        task = self._shutdown_task
        if task is None:
            # 检查与赋值之间没有挂起点，同一事件循环里只会创建一条清理任务。shield 让调用方取消
            # 自己的等待时不把 Runtime 留在拆到一半的状态。
            task = asyncio.create_task(self._shutdown_once(), name='runtime:shutdown')
            task.add_done_callback(_observe_shutdown_task)
            self._shutdown_task = task
        caller = cast('Runtime | None', chain[-1] if chain else None)
        if caller is not None and self._shutdown_reaches(caller):
            return
        if caller is not None:
            caller._shutdown_waits_for[self] = caller._shutdown_waits_for.get(self, 0) + 1
        try:
            await asyncio.shield(task)
        finally:
            if caller is not None:
                remaining = caller._shutdown_waits_for[self] - 1
                if remaining:
                    caller._shutdown_waits_for[self] = remaining
                else:
                    del caller._shutdown_waits_for[self]

    def _shutdown_reaches(self, target: Runtime) -> bool:
        """当前关机等待图里，从 self 出发能否走到 target。"""
        pending: list[Runtime] = [self]
        seen: set[Runtime] = set()
        while pending:
            runtime = pending.pop()
            if runtime is target:
                return True
            if runtime in seen:
                continue
            seen.add(runtime)
            pending.extend(runtime._shutdown_waits_for)
        return False

    async def _shutdown_once(self) -> None:
        """完整关机只跑一次；每个公开 `shutdown` 调用等待本任务的同一结果。"""
        # 本任务建在第一个调用方的上下文里；关机 hook 派的活不该按那个调用方的出处取 id、记在它的
        # span 上——谁先叫 shutdown 取决于调度（M3 D10）。
        becoming_owner(('shutdown',), {})
        outside_any_span()
        no_installer()
        token = _SHUTTING_DOWN.set((*_SHUTTING_DOWN.get(), self))
        try:
            await self._shutdown_body()
        finally:
            _SHUTTING_DOWN.reset(token)

    async def _shutdown_body(self) -> None:
        """执行关机步骤；与共享任务和重入检测分开。"""
        # 置位在 gather 之前：与本次关机并发的 spawn 既赶不上取消名单、事件又发不出去。
        self._closed = True
        self.broker.off_missing_queue(self._on_missing_queue)
        tasks = [
            task
            for agent_id in self._table.agent_ids()
            if (task := self._table.get_task(agent_id)) is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 收割任务不在实例表里，上面那份取消名单看不见它们；不等掉就是一串 "Task was destroyed but
        # it is pending"。清空之后再等：done-callback 要过一次事件循环才把它们摘掉，照着集合等会
        # 白转一圈。收割任务自己不会再排新的收割任务。
        while self._reapers:
            reapers = list(self._reapers)
            self._reapers.clear()
            await asyncio.gather(*reapers, return_exceptions=True)
        # 存盘排在取消 + gather 之后（spec §15 shutdown/SIGINT 存盘，F9）：此刻驱动都已停、取消路径
        # 的 _finish 不存盘，而 Agent 仍在实例表、轨迹已终态——读的是最终状态，不与在跑的驱动抢读，
        # 异步 store 也不会把半途快照盖掉干净那份。
        await self._checkpoint_all_sessions()
        await self.hooks._dispatch(Junction.RUNTIME_SHUTDOWN)  # pyright: ignore[reportPrivateUsage]
        failures: list[BaseException] = []
        try:
            failures.extend(await self._module_assembly.shutdown(self))
        finally:
            # 报在关掉总线之前与之后都不对：之前报不全（模块关闭里还有插桩），而关总线抛出时之后那句
            # 根本走不到。
            try:
                await self.events.close()
            finally:
                self._report_tracer_failures()
        if not failures:
            return
        if all(isinstance(failure, Exception) for failure in failures):
            raise ExceptionGroup(
                'Runtime Module 关闭失败',
                cast(list[Exception], failures),
            )
        raise BaseExceptionGroup('Runtime Module 关闭失败', failures)
