"""回放执行（M3 D11）：外部世界按记录给出，内核全真并逐项比对。

一份 v2 场景文档（手写的，或捕获的 `replay.json`）跑在真的 Runtime 上，只有触达外部世界的部分换成
记录：

- **模型：** 所有角色共用一个回放模型，按内核的「当前 LLM 调用」（agent id、轮、第几次）取记录
  的回复或错误，首次调用、`RetryModule` 的重试与备用模型各有各的键；回复带着逐单元指纹时，回放用
  同一份算法与记录下的规则集对自己发出的请求算一遍，第一个对不上的单元就是分叉的地方。
- **工具：** 触达外部世界的工具（`Read Write Edit Glob Grep Bash BashOutput KillBash` 与输出取决于
  时机的 `QueryAgent`）换成同名的替身，按调用位置交回记录的结局，不碰磁盘、不起进程；记录带着执行
  参数的指纹时先比参数。派活、消息、人机工具真跑。被 `PRE_TOOL_USE` 跳过的调用由一个回放装上的
  hook 原样再跳过。
- **危险判定：** 规则、处置表与 Flag 从宿主档案重建（工作区根用记录下的解析结果，`real=`），
  判定真跑；每一次闸门判定（含这次调用的结局）与记录逐项比对。
- **人与宿主：** 人的答复按请求的稳定键给出（手写文档也可以按先后与选择器给）；`timeout` 不答，
  让等待自己超时；`abort` 在它所在的请求处中止那个 Agent；`interrupt` / `shutdown` 在 run 安静
  下来的那一处发生（记录了它发生时在等哪些调用的，只在回放恰好停在那些调用上时发生）。
- **记录里没有的那一次：** 原始 run 在某次调用上被打断（Ctrl-C、关闭、被 kill、根结束后的关机）时，
  文档里正好没有它。所以没有记录的模型调用、工具调用与人的请求先停在那里，由宿主的动作或关机同样地
  取消；run 安静下来时还停着、又没有什么会来取消它，才算分叉。
- **时间：** 回放跑在一个虚拟时钟上：事件循环无事可做时时间直接跳到下一个定时器，等待超时与重试的
  退避一秒都不真等，而内核看到的截止时间照样自洽（不会冒出一条「broker 提前答了没有」的假 ERROR）。
  还有记录没用上时先让定时器走完（原始 run 是走过了它们才用上那些记录的），用完了才发出宿主的动作。
- **结束：** 根 Agent 结束后，别的 Agent 接着跑到安静下来（原始 run 的根结束时它们已经做完的那些
  事，回放里由于调度不同可能还没做），再像原始 run 那样关掉 Runtime。

真跑工具只给调用方明确允许的手写文档（`real_tools=True`）：子进程在跑时时间不能跳、也判不出安静，
所以那种回放用真时钟，没有记录的调用当场算分叉，不收 `allow_divergence` 与 interrupt / shutdown。

不一致记成 `Divergence`。没开 `allow_divergence` 时第一处分叉就停下（关掉 Runtime），免得一处分叉
引出一串看起来各不相干的分叉；开了就记下、尽量往下跑。没被用上的记录（回复、工具结局、跳过、人的
答复、记录里的超时、宿主的动作、id）也算分叉；文档里写了的 `expect` 照 v1 逐项比对。文档本身用不了
（payload 读不到、对不上、回放自己在替身里出错）时 `run` 抛出，而不是交回一个分叉。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import selectors
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from syndica.kernel import (
    DEFAULT_RULES,
    AgentCatalog,
    AgentFactory,
    AgentOptions,
    AgentStatus,
    BaseAgent,
    CausalityEdge,
    ConfigError,
    ContextBlock,
    DangerJudge,
    Disposition,
    EventBus,
    HookResult,
    HooksCenter,
    HostFacts,
    HumanInteractionModule,
    Junction,
    LLMConfig,
    LocalWorkspace,
    MailboxConfirmationHandler,
    ManifestSource,
    Message,
    Orchestrator,
    RetryModule,
    Runtime,
    RuntimeModule,
    SafetyModule,
    ScenarioDocument,
    SpanKind,
    SpanStatus,
    Subscription,
    SyndicaError,
    Tool,
    ToolCall,
    ToolResult,
    Tracer,
    TraceRecorder,
    TrustLevel,
    local_tools,
    register_builtins,
)
from syndica.kernel.context import ArtifactRef
from syndica.kernel.danger import (
    DangerRule,
    RuleTablePolicy,
    git_state_change_rule,
    path_outside_workspace_rule,
    workspace_root_removal_rule,
)
from syndica.kernel.errors import ContractViolation
from syndica.kernel.ids import StableKey, current_tool_call
from syndica.kernel.redaction import Redactor, RuleRedactor
from syndica.kernel.scenario_schema import (
    REPLAYED_TOOLS,
    DangerRuleSpec,
    HostAction,
    HostProfile,
    PayloadText,
    ScenarioError,
    ScenarioInstance,
    ScenarioReply,
    ScenarioResponse,
    ScenarioRole,
    SkippedCall,
    ToolOutcome,
)
from syndica.kernel.trace import AttributeValue
from syndica.kernel.trace_recorder import RequestFingerprints, arguments_digest
from syndica.kernel.tracing import NullTracer, current_llm_call, span_id_of
from syndica.runner.scenario import ScenarioEvent, ScenarioResult

__all__ = ['Divergence', 'ReplayResult', 'ReplayRunner']

logger = logging.getLogger(__name__)

# `host.no_write` 下放行的工具：只读的三件、派活与消息、问人。
_READ_ONLY_TOOLS = frozenset(
    {
        'Read',
        'Glob',
        'Grep',
        'Delegate',
        'Fork',
        'SendMessage',
        'KillAgent',
        'QueryAgent',
        'AskUser',
        'AskUserQuestion',
    }
)
_REQUESTS = MappingProxyType(
    {
        'user_input_requested': 'user_input',
        'user_question_requested': 'user_question',
        'tool_confirmation_requested': 'confirmation',
    }
)
_IMPLS: Mapping[str, type[BaseAgent]] = MappingProxyType(
    {'syndica.kernel.agents.orchestrator:Orchestrator': Orchestrator}
)
# 虚拟时钟下，无事可做、也没有定时器的事件循环再空等这么多次（每次真睡 50 ms）就判停滞：原始 run
# 里的线程（`to_thread`）此刻有机会回来，回不来就是回放卡住了。
_IDLE_SPINS = 40
_UNITS = ('模型参数', 'system', '工具定义')


@dataclass(frozen=True)
class Divergence:
    """一处分叉：哪一类（`request` `reply` `tool` `skip` `gate` `id` `response` `unconsumed`
    `outcome` `stall`）、在哪（Agent 选择器、轮次、调用位置或键）、怎么不一样。"""

    kind: str
    where: str
    detail: str


@dataclass(frozen=True)
class ReplayResult:
    """`divergences` 为空时这次回放复现了原始 run：所有记录都被用上、闸门判定逐项相同、`expect`
    全部成立。`result` 是回放观察到的结局（与 v1 的 `ScenarioResult` 同形）。`trajectories` 是回放
    里每个 Agent 的轨迹（按选择器；被打断的停在打断的那一处），捕获即回放拿它与原始 run 逐块比。
    `unrebuilt` 是宿主档案里没能重建的 Module 名，以及只有它们声明、因此没有交给 Runtime 的 Flag
    （`flags.<key>`）：宿主自己的 Module 的作用已经在记录里（例如被跳过的调用）。"""

    divergences: tuple[Divergence, ...]
    result: ScenarioResult
    unrebuilt: tuple[str, ...] = ()
    trajectories: Mapping[str, tuple[ContextBlock, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def reproduced(self) -> bool:
        return not self.divergences


class _VirtualClockLoop(asyncio.SelectorEventLoop):
    """时间只在无事可做时往前跳的事件循环：`time()` 是虚拟时间，选择器在有就绪回调时照常轮询；无事
    可做时先问回放要不要做点什么，不要就把时间直接推到最早的那个定时器。"""

    def __init__(self) -> None:
        super().__init__(selectors.DefaultSelector())
        self._now = 0.0
        self.on_idle: Callable[[bool], bool] | None = None
        self.on_stall: Callable[[], None] | None = None
        self._spins = 0
        selector = cast(Any, self)._selector
        real_select = selector.select

        def select(timeout: float | None = None) -> list[Any]:
            events = cast(list[Any], real_select(0))
            if events or timeout == 0:
                self._spins = 0
                return events
            if self.on_idle is not None and self.on_idle(timeout is not None):
                self._spins = 0
                return []
            if timeout is not None:
                self._spins = 0
                # 直接跳到那个定时器：asyncio 把一次 select 的超时截在一天，按超时往前推，远期的
                # 定时器要空转上亿次。
                scheduled = cast(list[asyncio.TimerHandle], cast(Any, self)._scheduled)
                target = scheduled[0].when()
                resolution = cast(float, cast(Any, self)._clock_resolution)
                if target + resolution <= target:
                    # 这个量级上加一个时钟精度不改变浮点数：asyncio 只在 `when < now + 精度` 时
                    # 让定时器就绪，停在 `when` 上它永远不就绪，循环空转。
                    target = math.nextafter(target, math.inf)
                self._now = max(self._now, target)
                return []
            self._spins += 1
            if self._spins > _IDLE_SPINS and self.on_stall is not None:
                self._spins = 0
                self.call_soon(self.on_stall)
                return []
            return cast(list[Any], real_select(0.05))

        selector.select = select

    def time(self) -> float:
        return self._now


class _ReplayTracer(NullTracer):
    """闸门判定（tool_call span 结束时带着的 `danger.*` 与 `tool.outcome`）按 span id 记下，各 Agent
    停下的原因按 agent span 记下；人的等待结束时告诉回放：它不再在等，或者记录里的那次超时到了。
    给了 `inner` 就把每一条插桩原样转给它（回放自己也记成一个 trace），回放自己的账先记。"""

    def __init__(
        self, ended: Callable[[str, str | None], None], inner: Tracer | None = None
    ) -> None:
        self.gates: dict[str, tuple[str, list[int], dict[str, AttributeValue]]] = {}
        self.stop_reasons: dict[str, str | None] = {}
        self._calls: dict[str, tuple[str, list[int]]] = {}
        self._agents: dict[str, str] = {}
        self._waits: dict[str, str] = {}
        self._ended = ended
        self._inner = inner

    def start_span(
        self,
        span_id: str,
        parent_span_id: str | None,
        kind: SpanKind,
        name: str,
        agent_id: str,
        attributes: dict[str, AttributeValue],
    ) -> None:
        if kind is SpanKind.TOOL_CALL:
            call = current_tool_call()
            position = [] if call is None else [call.cycle, call.turn, call.index]
            self._calls[span_id] = (agent_id, position)
        elif kind is SpanKind.AGENT:
            self._agents[span_id] = agent_id
        elif kind is SpanKind.WAIT and attributes.get('wait.kind') == 'human':
            target = attributes.get('wait.target')
            self._waits[span_id] = target if isinstance(target, str) else ''
        if self._inner is not None:
            self._inner.start_span(span_id, parent_span_id, kind, name, agent_id, attributes)

    def end_span(
        self, span_id: str, status: SpanStatus, attributes: dict[str, AttributeValue]
    ) -> None:
        call = self._calls.pop(span_id, None)
        if call is not None and 'danger.disposition' in attributes:
            self.gates[span_id] = (*call, dict(attributes))
        agent_id = self._agents.pop(span_id, None)
        if agent_id is not None:
            # 与捕获同一个判据：最后一个工作周期的原因，被取消的周期没有原因。
            reason = attributes.get('stop_reason')
            self.stop_reasons[agent_id] = reason if isinstance(reason, str) else None
        target = self._waits.pop(span_id, None)
        if target is not None:
            outcome = attributes.get('wait.outcome')
            self._ended(target, outcome if isinstance(outcome, str) else None)
        if self._inner is not None:
            self._inner.end_span(span_id, status, attributes)

    def add_event(self, span_id: str, name: str, attributes: dict[str, AttributeValue]) -> None:
        if self._inner is not None:
            self._inner.add_event(span_id, name, attributes)

    def add_edge(self, edge: CausalityEdge) -> None:
        if self._inner is not None:
            self._inner.add_edge(edge)

    def llm_exchange(
        self,
        span_id: str,
        request: Mapping[str, Any],
        reply: ContextBlock | None,
        error: BaseException | None,
    ) -> None:
        if self._inner is not None:
            self._inner.llm_exchange(span_id, request, reply, error)

    def tool_result(
        self,
        span_id: str,
        result: ToolResult | None,
        error: BaseException | None,
        *,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        if self._inner is not None:
            self._inner.tool_result(span_id, result, error, arguments=arguments)

    def wait_answer(self, span_id: str, message: Message) -> None:
        if self._inner is not None:
            self._inner.wait_answer(span_id, message)


@dataclass
class _Bound:
    """一个运行时 Agent 对应的那个实例。"""

    role: str
    index: int
    instance: ScenarioInstance
    replies: set[int] = field(default_factory=set[int])
    next_reply: int = 0


@dataclass
class _Missing:
    """一次停在「记录里没有」上的调用或请求。`future` 是停着的调用（人的请求没有：不答就是停着）；
    `fallback` 是允许分叉时拿来让它接着跑的值（只有工具有：交回一个说明）。"""

    divergence: Divergence
    future: asyncio.Future[Any] | None = None
    fallback: object = None
    reported: bool = False


class _Diverged:
    """`_response_for` 的第三种答案：有记录、但对不上，已经报过分叉。"""


_DIVERGED = _Diverged()


class ReplayRunner:
    """跑一份 v2 场景文档的回放。

    `payloads` 按 sha256 交回 trace 里的 payload 正文（文档里的 `{payload: …}`）；读不到、不是文本、
    或正文的 sha256 对不上，这份文档就用不了（`ConfigError`，回放开始之前就报）。`redactor` 是记录时
    的规则集，用来对回放自己的请求与执行的参数取指纹；不给就用只有内置规则的那一套。
    `allow_divergence` 时分叉只记下、不停。`perturb` 在每次模型调用与工具结局交回之前等一下（测试
    用来扰动调度）。

    `real_tools=True` 才会真跑工具：只对没有工具结局的手写文档起作用，工具在这台机器上、按文档里的
    危险规则跑。文档决定不了这件事——来路不明的文档按它自己的规则真跑，就是在执行它写的命令。
    """

    def __init__(
        self,
        *,
        payloads: Callable[[str], str | None] | None = None,
        redactor: Redactor | None = None,
        allow_divergence: bool = False,
        perturb: Callable[[], Awaitable[None]] | None = None,
        real_tools: bool = False,
    ) -> None:
        self._payloads = payloads
        self._redactor = RuleRedactor() if redactor is None else redactor
        self._allow = allow_divergence
        self._perturb = perturb
        self._real_tools = real_tools

    def run(
        self,
        document: ScenarioDocument,
        *,
        workspace_root: str | Path | None = None,
        recorder: TraceRecorder | None = None,
    ) -> ReplayResult:
        """同步跑完（回放自己建事件循环）。v1 文档交给 `ScenarioRunner`。给了 `recorder`（还没
        `begin` 的）就把这次回放本身也记下来：在回放的事件循环里 `begin` 与 `finish`，插桩照样转给
        它；它开不了头时记一条 WARNING，回放照常。"""
        if document.version != 2:
            raise ConfigError(
                expected='ReplayRunner 跑 version: 2 的场景文档',
                received=f'{document.source!r} 是 version: {document.version}',
                next_action='v1 的手写脚本用 ScenarioRunner().run(load_scenario(path)) 跑',
            )
        instances = [instance for role in document.roles.values() for instance in role.instances]
        # 捕获的文档（带 `source`）里没有某次调用的结局，说明它当时没执行（被阻断、被拒、被打断）：
        # 回放也不许真跑它。手写的文档不给工具结局时才可能真跑。
        stubbed = document.origin is not None or any(
            instance.tool_results or instance.skipped for instance in instances
        )
        external = sorted(
            {
                call.name
                for instance in instances
                for reply in instance.replies
                for call in reply.tool_calls
                if call.name in REPLAYED_TOOLS
            }
        )
        real = not stubbed and bool(external)
        if real:
            self._check_real(document, external)
        resolved = self._resolve(document)
        replay = _Replay(self, document, stubbed=stubbed, virtual=not real, resolved=resolved)
        with asyncio.Runner(loop_factory=None if real else _VirtualClockLoop) as runner:
            try:
                return runner.run(replay.run(workspace_root, recorder))
            except asyncio.CancelledError:
                if not replay.hung:
                    raise
        raise ContractViolation(
            expected='回放在第一处分叉停下之后，关掉 Runtime 就结束',
            received='停下之后事件循环仍然无事可做、也没有定时器：关闭 Runtime 没有完成',
            next_action='这是内核或回放的缺陷（某个任务不响应取消）：带上这份文档报告',
        )

    def _check_real(self, document: ScenarioDocument, external: Sequence[str]) -> None:
        if not self._real_tools:
            raise ConfigError(
                expected=(
                    '回放文档里每次触达外部的工具调用都有记录的结局（捕获的 trace 都有），'
                    '或者调用方明确允许真跑'
                ),
                received=f'{document.source!r} 没有工具结局，却调用了 {", ".join(external)}',
                next_action=(
                    '自己写的场景要真跑工具就传 ReplayRunner(real_tools=True)：工具在这台机器上照'
                    '文档里的危险规则跑；来路不明的文档不要这样跑'
                ),
            )
        if self._allow:
            raise ConfigError(
                expected='真跑工具的回放不开 allow_divergence',
                received='real_tools=True 且 allow_divergence=True',
                next_action=(
                    '真跑工具时判不出 run 停在了哪里（子进程可能还在跑），分叉之后没法接着比：'
                    '去掉 allow_divergence，或给工具补上记录的结局'
                ),
            )
        actions = [action.kind for action in document.host_actions if action.kind != 'abort']
        if actions:
            raise ConfigError(
                expected='真跑工具的回放文档不带 interrupt / shutdown',
                received=f'{document.source!r} 的 host_actions 有 {actions!r}',
                next_action=(
                    '它们在 run 安静下来的那一处发出，而真跑工具时判不出安静：给工具补上记录的'
                    '结局（tool_results），或删掉这两种动作'
                ),
            )

    def _resolve(self, document: ScenarioDocument) -> dict[str, str]:
        """读出文档引用的每一份 payload。在回放开始之前读完：在替身里才读，读不到的错误会被内核当成
        一次模型或工具失败吞掉，变成一个看起来正常的结局。"""
        resolved: dict[str, str] = {}
        for digest in sorted({ref.digest for ref in _payload_refs(document)}):
            text = None if self._payloads is None else self._payloads(digest)
            if text is None:
                raise ConfigError(
                    expected=f'回放文档引用的 payload {digest} 读得到',
                    received='没有给 payloads，或它交回 None',
                    next_action=(
                        '构造时给 ReplayRunner(payloads=...)：按 sha256 交回 payload 的正文'
                        '（trace 目录 payloads/<sha256>.txt 的文本）。'
                        '捕获写下的文档把正文内联，不引用 payload'
                    ),
                )
            given = cast(object, text)
            if not isinstance(given, str):
                raise ConfigError(
                    expected=f'payloads 交回 payload {digest} 的正文（str）',
                    received=f'交回 {type(given).__name__}',
                    next_action='按 sha256 交回 trace 里那个 payload 文件的文本（UTF-8 解码之后）',
                )
            try:
                actual = hashlib.sha256(given.encode('utf-8')).hexdigest()
            except UnicodeEncodeError:
                actual = None
            if actual != digest:
                raise ConfigError(
                    expected=f'payload {digest} 的正文的 sha256 就是这个名字',
                    received='读到的正文对不上它的名字',
                    next_action='这个 trace 的 payload 文件被改过或读错了：重新记录，不要拿它回放',
                )
            resolved[digest] = given
        return resolved


class _Replay:
    def __init__(
        self,
        runner: ReplayRunner,
        document: ScenarioDocument,
        *,
        stubbed: bool,
        virtual: bool,
        resolved: Mapping[str, str],
    ) -> None:
        self._runner = runner
        self._document = document
        self._stubbed = stubbed
        self._virtual = virtual
        self._resolved = dict(resolved)
        self._host = document.host if document.host is not None else HostProfile()
        self._fingerprints = RequestFingerprints(runner._redactor)  # pyright: ignore[reportPrivateUsage]
        self._ids = {key: value for key, value in document.ids}
        self._keys = {value: key for key, value in document.ids}
        self._keyed = bool(document.ids)
        self.divergences: list[Divergence] = []
        self._bound: dict[str, _Bound] = {}
        self._by_key: dict[StableKey, tuple[str, int]] = {}
        for role in document.roles.values():
            for index, instance in enumerate(role.instances):
                if instance.key is not None:
                    self._by_key[instance.key] = (role.name, index)
        self._unkeyed: dict[str, int] = {name: 0 for name in document.roles}
        self._used_ids: set[StableKey] = set()
        self._used_outcomes: set[tuple[str, tuple[int, int, int]]] = set()
        self._used_skips: set[tuple[str, tuple[int, int, int]]] = set()
        self._answered: set[int] = set()
        self._ordered = 0
        self._aborted: set[StableKey] = set()
        self._timeouts_due: dict[StableKey, str] = {}
        self._pending = [action for action in document.host_actions if action.kind != 'abort']
        self._waiting: dict[StableKey, _Missing] = {}
        self._stopped = False
        self._halted = False
        self.hung = False
        self._failure: BaseException | None = None
        self._main: asyncio.Future[Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._draining: asyncio.Future[None] | None = None
        self._runtime: Runtime | None = None
        self._errors: dict[str, str] = {}
        self._trajectories: dict[str, tuple[ContextBlock, ...]] = {}

    # --- divergence --------------------------------------------------------------------------

    @property
    def _allow(self) -> bool:
        return self._runner._allow  # pyright: ignore[reportPrivateUsage]

    def _diverge(self, kind: str, where: str, detail: str) -> None:
        self.divergences.append(Divergence(kind, where, detail))
        if not self._allow:
            # 停在第一处分叉：此后没跑完的记录与没成立的 `expect` 都是它的后果，不再各报一遍。
            self._halted = True
            self._stop()

    def _stop(self) -> None:
        self._stopped = True
        if self._main is not None and not self._main.done():
            self._main.cancel()

    def _engine_failed(self, error: BaseException) -> None:
        """回放自己在替身里出错（文档里的值建不出内核要的东西）：内核会把它当成一次模型或工具失败
        吞掉，所以记下、停下，`run` 收尾之后原样抛出。"""
        if self._failure is None:
            self._failure = error
        self._halted = True
        self._stop()

    async def _parked(self) -> Any:
        """停下之后来的调用停在这里，直到关机把它取消：不往下跑就不会引出一串分叉。"""
        await asyncio.get_running_loop().create_future()

    async def _missing(
        self, key: StableKey, divergence: Divergence, fallback: object = None
    ) -> Any:
        """记录里没有这一次：先停在这里（见模块说明），run 安静下来时还停着才算分叉。真时钟下判不出
        安静，当场算。"""
        if not self._virtual:
            self._diverge(divergence.kind, divergence.where, divergence.detail)
            return await self._parked()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[key] = _Missing(divergence, future, fallback)
        try:
            return await future
        finally:
            self._waiting.pop(key, None)

    # --- documents ---------------------------------------------------------------------------

    def _text(self, value: object) -> Any:
        """把文档里的值还原成交给内核的样子：payload 引用换成读出来的正文，元组与只读映射成列表与
        字典。"""
        if isinstance(value, PayloadText):
            return self._resolved[value.digest]
        if isinstance(value, Mapping):
            return {
                str(key): self._text(item)
                for key, item in cast(Mapping[object, object], value).items()
            }
        if isinstance(value, tuple):
            return [self._text(item) for item in cast(tuple[object, ...], value)]
        return value

    def _selector(self, agent_id: str) -> str:
        bound = self._bound.get(agent_id)
        return f'<unknown:{agent_id}>' if bound is None else f'{bound.role}[{bound.index}]'

    # --- ids ----------------------------------------------------------------------------------

    def issue(self, key: StableKey, what: str) -> str:
        if not self._keyed:
            return uuid.uuid4().hex
        found = self._ids.get(tuple(key))
        if found is None:
            self._diverge(
                'id',
                f'{what} {list(key)!r}',
                '记录里没有这个稳定键：回放创建了原始 run 没有创建的东西',
            )
            return uuid.uuid4().hex
        self._used_ids.add(tuple(key))
        return found

    def _bind(self, role: str, agent_id: str) -> None:
        if self._keyed:
            key = self._keys.get(agent_id)
            located = None if key is None else self._by_key.get(key)
            if located is None or located[0] != role:
                return
            index = located[1]
        else:
            index = self._unkeyed[role]
            self._unkeyed[role] += 1
            if index >= len(self._document.roles[role].instances):
                return
        self._bound[agent_id] = _Bound(role, index, self._document.roles[role].instances[index])

    # --- model ------------------------------------------------------------------------------

    async def reply(
        self, agent_id: str, turn: int, attempt: int, request: Mapping[str, Any]
    ) -> ContextBlock:
        perturb = self._runner._perturb  # pyright: ignore[reportPrivateUsage]
        if perturb is not None:
            await perturb()
        if self._stopped:
            return await self._parked()
        where = f'{self._selector(agent_id)} 第 {turn} 轮第 {attempt} 次调用'
        try:
            bound = self._bound.get(agent_id)
            if bound is None:
                # 原始 run 里根本没有这个 Agent（不是在它的某次调用上被打断）：当场就是分叉。
                self._diverge('reply', where, '这个 Agent 在文档里没有对应的实例')
                return await self._parked()
            found = self._recorded_reply(bound, turn, attempt)
            if found is None:
                missing = Divergence('reply', where, '记录里没有这一次模型调用')
                return await self._missing(('llm_call', agent_id, turn, attempt), missing)
            index, reply = found
            bound.replies.add(index)
            if reply.request_digests is not None:
                self._compare_request(where, reply.request_digests, request)
                if self._stopped:
                    return await self._parked()
            replied: ContextBlock | Exception = (
                self._exception(reply.raises)
                if reply.raises is not None
                else ContextBlock(
                    role='assistant',
                    content=self._text(reply.content),
                    tool_calls=[
                        ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                        for call in reply.tool_calls
                    ],
                    trust=TrustLevel(reply.trust),
                    extra={} if reply.extra is None else self._text(reply.extra),
                )
            )
        except Exception as error:
            self._engine_failed(error)
            return await self._parked()
        if isinstance(replied, Exception):
            raise replied
        return replied

    def _recorded_reply(
        self, bound: _Bound, turn: int, attempt: int
    ) -> tuple[int, ScenarioReply] | None:
        replies = bound.instance.replies
        if any(reply.turn is not None for reply in replies):
            # 键在一个实例里唯一（schema 保证），内核也不会把同一个键问两遍。
            for index, reply in enumerate(replies):
                if (reply.turn, reply.attempt) == (turn, attempt):
                    return index, reply
            return None
        if bound.next_reply >= len(replies):
            return None
        index = bound.next_reply
        bound.next_reply += 1
        return index, replies[index]

    def _compare_request(
        self, where: str, recorded: Sequence[str], request: Mapping[str, Any]
    ) -> None:
        actual = self._fingerprints.of(request)
        for index in range(max(len(recorded), len(actual))):
            want = recorded[index] if index < len(recorded) else None
            got = actual[index] if index < len(actual) else None
            if want != got:
                unit = _UNITS[index] if index < len(_UNITS) else f'第 {index - len(_UNITS)} 条历史'
                detail = (
                    f'请求的「{unit}」与记录不同'
                    if want is not None and got is not None
                    else (
                        f'请求有 {len(actual)} 个单元，记录有 {len(recorded)} 个'
                        f'（从「{unit}」起对不上）'
                    )
                )
                self._diverge('request', where, detail)
                return

    def _exception(self, error: ScenarioError) -> Exception:
        """记录下的那个异常：类名、模块与文字照原样（它们会进上下文与 `errors`）；内核自己的类型
        保留身份，内核按类型分支的地方（超窗、工具的致命错误）照旧分支。"""
        message = cast(str, self._text(error.message))
        module, _, name = error.type.rpartition('.')
        base = _known_error(error.type)

        def text(_: object) -> str:
            return message

        namespace: dict[str, Any] = {'__module__': module, '__qualname__': name, '__str__': text}
        try:
            replayed = cast(Any, type(name, (base,), namespace))
            if issubclass(base, SyndicaError):
                exception = cast(
                    Exception,
                    replayed(
                        expected='记录下的失败', received=message, next_action='回放照原样交回'
                    ),
                )
            else:
                exception = cast(Exception, replayed(message))
        except Exception:
            # 构造签名特殊的类型（`UnicodeDecodeError` 要五个参数）退回普通异常：名字与文字照旧。
            exception = cast(Exception, type(name, (Exception,), namespace)(message))
        for attribute, value in (
            ('status_code', error.status_code),
            ('retry_after', error.retry_after),
        ):
            if value is not None:
                setattr(exception, attribute, value)
        return exception

    # --- tools ------------------------------------------------------------------------------

    async def outcome(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        perturb = self._runner._perturb  # pyright: ignore[reportPrivateUsage]
        if perturb is not None:
            await perturb()
        if self._stopped:
            return await self._parked()
        call = current_tool_call()
        if call is None:
            raise ContractViolation(
                expected=f'回放的 {name} 只在内核的一次工具调用里执行',
                received='没有「当前工具调用」',
                next_action='不要直接调用回放的工具；它按调用位置取记录',
            )
        position = (call.cycle, call.turn, call.index)
        where = f'{self._selector(call.agent_id)} {name} {list(position)!r}'
        try:
            bound = self._bound.get(call.agent_id)
            found = (
                None
                if bound is None
                else next(
                    (item for item in bound.instance.tool_results if item.call == position), None
                )
            )
            if found is None:
                # 允许分叉时尽量往下跑：交回一个说明，后面的闸门判定与结局照样拿来比。
                placeholder = ToolResult(
                    output='（回放：记录里没有这次调用的结局，它在原始 run 里没有执行完）',
                    trust=TrustLevel.UNTRUSTED,
                )
                missing = Divergence('tool', where, '记录里没有这次调用的结局')
                return await self._missing(call.key, missing, placeholder)
            self._used_outcomes.add((call.agent_id, position))
            if found.arguments_digest is not None:
                actual = arguments_digest(arguments, self._runner._redactor)  # pyright: ignore[reportPrivateUsage]
                if actual != found.arguments_digest:
                    self._diverge(
                        'tool',
                        where,
                        '执行的参数与记录不同（hook、确认或闸门之后交给工具的那一份）',
                    )
                    if self._stopped:
                        return await self._parked()
            if any(call.key in (action.waiting or ()) for action in self._pending):
                # 原始 run 在这次调用交回之后、结果落进轨迹之前被打断（宿主的 hook 还在跑）：停在
                # 这里，等那个打断同样地取消它。
                cut = Divergence(
                    'tool',
                    where,
                    '原始 run 在这次调用的结果落进轨迹之前被打断，回放没有在这里被打断',
                )
                return await self._missing(call.key, cut)
            result: ToolResult | Exception = (
                self._exception(found.raised) if found.raised is not None else self._result(found)
            )
        except Exception as error:
            self._engine_failed(error)
            return await self._parked()
        if isinstance(result, Exception):
            raise result
        return result

    def _result(self, recorded: ToolOutcome | SkippedCall) -> ToolResult:
        return ToolResult(
            output=self._text(recorded.output),
            meta=self._text(recorded.meta),
            artifacts=[
                ArtifactRef(**cast(dict[str, Any], self._text(item))) for item in recorded.artifacts
            ],
            trust=TrustLevel(recorded.trust),
        )

    def _tools(self, workspace: LocalWorkspace) -> dict[str, Tool]:
        real = local_tools(workspace.portal(), output_limit=self._host.output_limit)
        if not self._stubbed:
            return real
        return {name: _stub(tool, self) for name, tool in real.items()}

    async def skip_recorded(self, **_: Any) -> HookResult:
        call = current_tool_call()
        if call is None:
            return HookResult()
        try:
            bound = self._bound.get(call.agent_id)
            position = (call.cycle, call.turn, call.index)
            found = (
                None
                if bound is None
                else next((item for item in bound.instance.skipped if item.call == position), None)
            )
            if found is None:
                return HookResult()
            self._used_skips.add((call.agent_id, position))
            return HookResult(skip=True, result=self._result(found))
        except Exception as error:
            self._engine_failed(error)
            return await self._parked()

    async def refuse_writes(self, **kwargs: Any) -> HookResult:
        """`host.no_write`：原始 run 里写操作会被宿主的只读闸跳过，那次跳过应当在记录里。"""
        call = current_tool_call()
        tool_call = kwargs.get('tool_call')
        name = tool_call.name if isinstance(tool_call, ToolCall) else None
        if call is None or name is None or name in _READ_ONLY_TOOLS:
            return HookResult()
        where = f'{self._selector(call.agent_id)} {name} {[call.cycle, call.turn, call.index]!r}'
        self._diverge('skip', where, 'host.no_write 下这次写操作本该被拦下，记录里却没有它被跳过')
        return HookResult(
            skip=True,
            result=ToolResult(output='只读 run：这次写操作没有执行。', trust=TrustLevel.TRUSTED),
        )

    # --- host profile ----------------------------------------------------------------------

    def _danger(self) -> tuple[DangerJudge | None, RuleTablePolicy | None]:
        rules = self._host.danger_rules
        judge = None if rules is None else DangerJudge(tuple(_rule(spec) for spec in rules))
        table = self._host.danger_table
        policy = (
            None
            if table is None
            else RuleTablePolicy({key: Disposition(value) for key, value in table.items()})
        )
        return judge, policy

    def _modules(self, llm: _ReplayLLM) -> tuple[tuple[RuntimeModule, ...], tuple[str, ...]]:
        names = self._host.modules
        if names is None:
            return (SafetyModule(), HumanInteractionModule()), ()
        fallbacks = str(self._host.flags.get('llm_fallbacks', ''))
        builders: dict[str, Callable[[], RuntimeModule]] = {
            'safety': SafetyModule,
            'human-interaction': HumanInteractionModule,
            'retry': lambda: RetryModule(
                {name.strip(): llm for name in fallbacks.split(',') if name.strip()}
            ),
        }
        built = tuple(builders[name]() for name in names if name in builders)
        return built, tuple(name for name in names if name not in builders)

    def _flags(
        self, modules: Sequence[RuntimeModule]
    ) -> tuple[dict[str, bool | int | str], tuple[str, ...]]:
        """交给 Runtime 的 Flag：只有重建出来的 Module 声明的那些。没重建的 Module 的 Flag 交给
        Runtime 会被拒（它只收已装 Module 声明的 key），它们的作用已经在记录里。"""
        declared = {flag.key for module in modules for flag in module.flags}
        flags = {key: value for key, value in self._host.flags.items() if key in declared}
        dropped = tuple(sorted(key for key in self._host.flags if key not in declared))
        return flags, dropped

    def _factories(self, tools: Mapping[str, Tool], llm: _ReplayLLM) -> dict[str, AgentFactory]:
        judge, policy = self._danger()
        catalog = AgentCatalog()
        register_builtins(catalog)
        source = Path(self._document.source)
        for role in self._document.roles.values():
            if role.manifest is not None:
                manifest = catalog.load_mapping(
                    self._text(role.manifest),
                    source=ManifestSource(
                        kind='yaml_file',
                        location=f'{source}#agents.{role.name}.manifest',
                        origin=str(source.parent.resolve()),
                    ),
                )
                if manifest.name != role.name:
                    raise ConfigError(
                        expected=f'agents 键与 manifest.name 都是 {role.name!r}',
                        received=f'manifest.name={manifest.name!r}',
                        next_action='让两处名字逐字相同',
                    )
        models = {catalog.get(name).model: llm for name in catalog.names()}
        frozen_catalog = catalog.freeze(
            tools,
            models,
            self._host.host_facts or HostFacts(os='scenario', platform='scenario', shell='/bin/sh'),
            danger_judge=judge,
            danger_policy=policy,
        )
        factories: dict[str, AgentFactory] = {}
        for role in self._document.roles.values():
            if role.frozen is not None:
                factories[role.name] = self._frozen_factory(role, tools, llm, judge, policy)
            else:
                factories[role.name] = self._manifest_factory(role, frozen_catalog[role.name], llm)
        return factories

    def _frozen_factory(
        self,
        role: ScenarioRole,
        tools: Mapping[str, Tool],
        llm: _ReplayLLM,
        judge: DangerJudge | None,
        policy: RuleTablePolicy | None,
    ) -> AgentFactory:
        frozen = role.frozen
        assert frozen is not None
        cls = BaseAgent if frozen.impl is None else _IMPLS[frozen.impl]
        options = AgentOptions(
            name=role.name,
            llm=llm,
            tools={name: tools[name] for name in frozen.tools if name in tools},
            system_prompt=cast(str, self._text(frozen.system_prompt)),
            max_turns=frozen.max_turns,
            danger_judge=judge,
            danger_policy=policy,
        )

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return self._prepared(role.name, cls(options, hooks=hooks, events=events))

        return build

    def _manifest_factory(
        self, role: ScenarioRole, base: AgentFactory, llm: _ReplayLLM
    ) -> AgentFactory:
        template_agent = base(HooksCenter(), EventBus())
        template = template_agent.options
        cap = self._host.max_turns
        turns = template.max_turns
        if cap is not None:
            turns = cap if turns is None else min(turns, cap)
        options = replace(template, llm=llm, tools=dict(template.tools), max_turns=turns)
        cls = type(template_agent)

        def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
            return self._prepared(role.name, cls(options, hooks=hooks, events=events))

        return build

    def _prepared(self, role: str, agent: BaseAgent) -> BaseAgent:
        self._bind(role, agent.agent_id)
        agent.register_hook(Junction.PRE_TOOL_USE, self.skip_recorded)
        if self._host.no_write:
            agent.register_hook(Junction.PRE_TOOL_USE, self.refuse_writes)
        if self._stubbed:
            # `QueryAgent` 由 Runtime 自己装（编排五件套），不经工厂交进来的工具表：它装进来的那一
            # 刻换成替身。
            install = agent.install_runtime_tools

            def install_stubbed(tools: Mapping[str, Tool]) -> None:
                install({name: _stub(tool, self) for name, tool in tools.items()})

            cast(Any, agent).install_runtime_tools = install_stubbed
        selector = self._selector(agent.agent_id)

        async def capture_failure(**kwargs: object) -> None:
            error = kwargs.get('error')
            if isinstance(error, BaseException):
                self._errors.setdefault(selector, f'{type(error).__name__}: {error}')

        agent.register_hook(Junction.LLM_FAILURE, capture_failure)
        return agent

    # --- humans and host actions -------------------------------------------------------------

    async def _answer(self, runtime: Runtime, subscription: Subscription) -> list[ScenarioEvent]:
        events: list[ScenarioEvent] = []
        async for event in subscription:
            events.append(
                ScenarioEvent(
                    sequence=event.sequence,
                    kind=event.kind,
                    agent=self._selector(event.agent_id),
                    payload=MappingProxyType(dict(event.payload)),
                )
            )
            kind = _REQUESTS.get(event.kind)
            if kind is None:
                continue
            raw_ref = event.payload.get('request_ref')
            key = None if raw_ref is None else tuple(cast(list[str | int], raw_ref))
            if key is not None and any(
                action.kind == 'abort' and action.key == key
                for action in self._document.host_actions
            ):
                self._aborted.add(key)
                await runtime.abort(event.agent_id)
                continue
            where = f'{self._selector(event.agent_id)} {kind}'
            response = self._response_for(kind, key, event.agent_id, event.payload)
            if isinstance(response, _Diverged):
                continue
            if response is None:
                if not self._stopped:
                    self._unanswered(where, key, event.agent_id, event.sequence)
                continue
            if response.kind == 'timeout':
                if key is not None:
                    self._timeouts_due[key] = where
                continue
            delivered = await runtime.send(
                event.agent_id,
                cast(str, self._text(response.answer)),
                activate=False,
                sender='human',
                correlation_id=cast(str, event.payload['request_id']),
                trust=TrustLevel(response.trust),
            )
            if not delivered.ok:
                self._diverge('response', where, f'人的答复没能投递：{delivered.reason}')
        return events

    def _unanswered(
        self, where: str, key: tuple[str | int, ...] | None, agent_id: str, sequence: int
    ) -> None:
        """记录里没有这个请求的答复：不答就是停着（原始 run 在它等着的时候被打断）。"""
        responses = self._document.responses
        keyed = not responses or responses[0].key is not None
        detail = (
            f'记录里没有请求 {list(key)!r} 的答复'
            if key is not None and keyed
            else '记录里的答复已经用完'
        )
        if not self._virtual:
            self._diverge('response', where, detail)
            return
        waiting = key if key is not None else ('request', agent_id, sequence)
        self._waiting[waiting] = _Missing(Divergence('response', where, detail))

    def _response_for(
        self,
        kind: str,
        key: tuple[str | int, ...] | None,
        agent_id: str,
        payload: Mapping[str, Any],
    ) -> ScenarioResponse | _Diverged | None:
        """这个请求在记录里的答复。没有时交回 None（由调用方让它停着）；有、但对不上的当场算分叉，
        交回 `_DIVERGED`。"""
        responses = self._document.responses
        where = f'{self._selector(agent_id)} {kind}'
        if responses and responses[0].key is not None:
            for index, response in enumerate(responses):
                if response.key == key and index not in self._answered:
                    self._answered.add(index)
                    if response.kind not in (kind, 'timeout'):
                        self._diverge(
                            'response',
                            where,
                            f'记录里这个请求的答复是 {response.kind}，回放来的是 {kind} 请求',
                        )
                        return _DIVERGED
                    return response
            return None
        if self._ordered >= len(responses):
            return None
        response = responses[self._ordered]
        matches = (
            response.kind == kind
            and response.agent == self._selector(agent_id)
            and (
                response.tool_call_id is None
                or response.tool_call_id == payload.get('tool_call_id')
            )
        )
        if not matches:
            self._diverge(
                'response',
                where,
                f'按先后轮到的答复是 {response.kind} / {response.agent} / {response.tool_call_id}',
            )
            return _DIVERGED
        self._answered.add(self._ordered)
        self._ordered += 1
        return response

    def wait_ended(self, target: str, outcome: str | None) -> None:
        """一次人的等待结束了（`_ReplayTracer` 交来）：它不再停着；记录里是超时的，那次超时到了。"""
        try:
            key = tuple(cast(list[str | int], json.loads(target))) if target else None
        except (ValueError, TypeError):
            key = None
        if key is None:
            return
        missing = self._waiting.pop(key, None)
        if outcome != 'timeout':
            return
        if self._timeouts_due.pop(key, None) is None and missing is not None:
            self._diverge(
                'response',
                missing.divergence.where,
                '这个请求在回放里等到了超时；记录里没有它的答复，原始 run 没等到这一刻',
            )

    def _leftovers(self) -> list[Divergence]:
        """还没用上的记录：回复、工具结局、跳过、没被创建的实例、人的答复、记录里的超时、请求处的
        中止（不含 id 与 interrupt / shutdown）。「还有没有记录」与收尾的报告共用这一份，两处不会
        对「什么算记录」各说各的。"""
        found: list[Divergence] = []
        bound_at = {(bound.role, bound.index): agent_id for agent_id, bound in self._bound.items()}
        for role in self._document.roles.values():
            for index, instance in enumerate(role.instances):
                selector = f'{role.name}[{index}]'
                agent_id = bound_at.get((role.name, index))
                if agent_id is None:
                    found.append(Divergence('unconsumed', selector, '这个实例在回放里没有被创建'))
                    continue
                used = self._bound[agent_id].replies
                unused = [number for number in range(len(instance.replies)) if number not in used]
                if unused:
                    found.append(
                        Divergence(
                            'unconsumed',
                            selector,
                            f'{len(unused)} 条记录的模型回复没用上（turns 第 {unused} 项）',
                        )
                    )
                found.extend(
                    Divergence(
                        'unconsumed', f'{selector} {list(item.call)!r}', '这次调用的记录结局没用上'
                    )
                    for item in instance.tool_results
                    if (agent_id, item.call) not in self._used_outcomes
                )
                found.extend(
                    Divergence(
                        'unconsumed',
                        f'{selector} {list(item.call)!r}',
                        '这次被跳过的调用在回放里没有出现',
                    )
                    for item in instance.skipped
                    if (agent_id, item.call) not in self._used_skips
                )
        found.extend(
            Divergence('unconsumed', f'responses[{index}]', f'{response.kind} 的答复没用上')
            for index, response in enumerate(self._document.responses)
            if index not in self._answered
        )
        found.extend(
            Divergence('unconsumed', where, '记录里这个请求等到了超时，回放里没有等到')
            for where in self._timeouts_due.values()
        )
        found.extend(
            Divergence('unconsumed', f'host_actions abort {list(action.key or ())!r}', '没有发生')
            for action in self._document.host_actions
            if action.kind == 'abort' and action.key not in self._aborted
        )
        return found

    def on_idle(self, timers: bool) -> bool:
        """事件循环无事可做时问一声。交回 True 表示安排了事（循环接着转、不推时间）；False 让循环把
        时间推到下一个定时器（`timers`），或者没有定时器时开始数空转。"""
        if self._runtime is None or self._stopped:
            return False
        leftovers = self._leftovers()
        left = bool(leftovers)
        if timers and left:
            return False
        if self._draining is not None:
            if self._draining.done():
                return False
            # 根已经结束：此刻还停着的调用正是原始 run 的关机切断的那些，像它那样关掉。
            self._draining.set_result(None)
            return True
        if self._pending and not left and self._ready_for(self._pending[0]):
            action = self._pending.pop(0)
            asyncio.get_running_loop().call_soon(self._host_action, action)
            return True
        if timers:
            return False
        return self._quiet(leftovers)

    def _ready_for(self, action: HostAction) -> bool:
        if action.waiting is None:
            return True
        return set(action.waiting) == set(self._waiting)

    def _quiet(self, leftovers: Sequence[Divergence]) -> bool:
        """没有定时器、也没有能发出的宿主动作：run 停在这里了。交回 True 表示做了处置。"""
        if self._pending:
            action = self._pending.pop(0)
            if leftovers:
                named = '、'.join(item.where for item in leftovers[:3])
                more = '等' if len(leftovers) > 3 else ''
                detail = (
                    f'回放安静下来时还有记录没用上（{named}{more}）；'
                    f'原始 run 用完了它们才发生 {action.kind}'
                )
            else:
                recorded = sorted(list(key) for key in action.waiting or ())
                current = sorted(list(key) for key in self._waiting)
                detail = (
                    f'原始 run 发生 {action.kind} 时在等 {recorded!r}；'
                    f'回放安静下来时在等 {current!r}'
                )
            self.divergences.append(Divergence('stall', f'host_actions {action.kind}', detail))
            self._halted = self._halted or not self._allow
            self._stop()
            return True
        fresh = [missing for missing in self._waiting.values() if not missing.reported]
        if not fresh:
            return False
        for missing in fresh:
            missing.reported = True
            self.divergences.append(missing.divergence)
        if self._allow:
            resumed = False
            for missing in fresh:
                future = missing.future
                if missing.fallback is not None and future is not None and not future.done():
                    future.set_result(missing.fallback)
                    resumed = True
            if resumed:
                return True
        else:
            self._halted = True
        self._stop()
        return True

    def _host_action(self, action: HostAction) -> None:
        del action
        self._stop()

    def on_stall(self) -> None:
        if not self._stopped:
            self.divergences.append(
                Divergence(
                    'stall',
                    'run',
                    '回放卡住了：没有能往下跑的事，也没有定时器，也没有停在记录之外的调用',
                )
            )
            self._halted = True
            self._stop()
            return
        # 停下之后还卡着：关掉 Runtime 没有完成。放弃这次回放，`run` 报出来。
        if self._task is not None and not self._task.done():
            self.hung = True
            self._task.cancel()

    # --- the run ----------------------------------------------------------------------------

    async def run(
        self, workspace_root: str | Path | None, recorder: TraceRecorder | None = None
    ) -> ReplayResult:
        loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if isinstance(loop, _VirtualClockLoop):
            loop.on_idle = self.on_idle
            loop.on_stall = self.on_stall
        recording = await _begun(recorder)
        status = SpanStatus.ERROR
        try:
            result = await self._run(workspace_root, recording)
            status = SpanStatus.OK
            return result
        except asyncio.CancelledError:
            status = SpanStatus.CANCELLED
            raise
        finally:
            if recording is not None:
                await recording.finish(status)
            if isinstance(loop, _VirtualClockLoop):
                loop.on_idle = None
                loop.on_stall = None

    async def _run(
        self, workspace_root: str | Path | None, recording: TraceRecorder | None = None
    ) -> ReplayResult:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        if workspace_root is None:
            temporary = tempfile.TemporaryDirectory(prefix='syndica-replay-')
            root = Path(temporary.name)
        else:
            root = Path(workspace_root)
            root.mkdir(parents=True, exist_ok=True)
        workspace = LocalWorkspace(str(root), shell='/bin/sh', env={})
        llm = _ReplayLLM(self)
        tracer = _ReplayTracer(self.wait_ended, recording)
        unrebuilt: tuple[str, ...] = ()
        events: list[ScenarioEvent] = []
        try:
            modules, unrebuilt_modules = self._modules(llm)
            flags, dropped = self._flags(modules)
            unrebuilt = unrebuilt_modules + tuple(f'flags.{key}' for key in dropped)
            factories = self._factories(self._tools(workspace), llm)
            runtime = Runtime(
                agents=factories,
                orchestration=True,
                modules=modules,
                flags=flags,
                confirmation_handler=MailboxConfirmationHandler(timeout=300),
                ids=_ReplayIds(self) if self._keyed else None,
                tracer=tracer,
            )
            self._runtime = runtime
            subscription = runtime.events.subscribe()
            answering = asyncio.create_task(self._answer(runtime, subscription))
            try:
                self._main = asyncio.ensure_future(self._drive(runtime, factories))
                try:
                    await self._main
                except asyncio.CancelledError:
                    if not self._stopped or self.hung:
                        raise
                await runtime.shutdown()
                events = await answering
                result = await self._observed(runtime, events, root, subscription, tracer)
            finally:
                if not answering.done():
                    await runtime.shutdown()
                    await asyncio.gather(answering, return_exceptions=True)
                subscription.close()
        finally:
            await workspace.close()
            if temporary is not None:
                temporary.cleanup()
        if self._failure is not None:
            raise self._failure
        if not self._halted:
            self._compare_gates(tracer)
            self._report_unconsumed()
            self._compare_expect(result)
        return ReplayResult(
            tuple(self.divergences),
            result,
            unrebuilt,
            MappingProxyType(dict(self._trajectories)),
        )

    async def _drive(self, runtime: Runtime, factories: Mapping[str, AgentFactory]) -> None:
        root_id = await self._enter(runtime, factories)
        await runtime.wait(root_id)
        if self._virtual:
            # 原始 run 在根结束之后立刻关机；那之前别的 Agent 做完的事，回放也让它们做完（见模块
            # 说明「结束」）。
            self._draining = asyncio.get_running_loop().create_future()
            await self._draining

    async def _enter(self, runtime: Runtime, factories: Mapping[str, AgentFactory]) -> str:
        entry = self._document.entry
        prompt = cast(str, self._text(entry.prompt))
        trust = TrustLevel(entry.trust)
        if entry.mode == 'query':
            return await runtime.spawn(
                factories[cast(str, entry.agent)], query=prompt, query_trust=trust
            )
        if entry.mode == 'send':
            agent_id = await runtime.spawn(factories[cast(str, entry.agent)])
            await runtime.send(agent_id, prompt, trust=trust)
            return agent_id
        topology = self._document.topology
        assert topology is not None
        slot_ids: dict[str, str] = {}
        for slot in topology.slots:
            if slot.boot == 'eager':
                slot_ids[slot.name] = await runtime.spawn(
                    factories[slot.agent], exit_mode=slot.exit_mode, metadata=dict(slot.metadata)
                )
        slot = next(slot for slot in topology.slots if slot.name == entry.slot)
        agent_id = slot_ids.get(slot.name)
        if agent_id is None:
            agent_id = await runtime.spawn(
                factories[slot.agent], exit_mode=slot.exit_mode, metadata=dict(slot.metadata)
            )
        await runtime.send(agent_id, prompt, trust=trust)
        return agent_id

    async def _observed(
        self,
        runtime: Runtime,
        events: list[ScenarioEvent],
        root: Path,
        subscription: Subscription,
        tracer: _ReplayTracer,
    ) -> ScenarioResult:
        statuses: dict[str, str] = {}
        final_output: dict[str, str] = {}
        tool_order: dict[str, tuple[str, ...]] = {}
        stop_reasons: dict[str, str] = {}
        for info in await runtime.registry.list():
            if info.agent_id not in self._bound:
                continue
            selector = self._selector(info.agent_id)
            statuses[selector] = info.status.value
            try:
                context = await runtime.wait(info.agent_id)
            except SyndicaError:
                continue
            # 被打断的也收：停在打断的那一处的轨迹同样要与原始 run 逐块相同。
            self._trajectories[selector] = tuple(context.current_traj)
            if info.status is not AgentStatus.ZOMBIE:
                continue
            assistants = [
                block.content
                for block in context.current_traj
                if block.role == 'assistant' and isinstance(block.content, str)
            ]
            if assistants:
                final_output[selector] = assistants[-1]
            tool_order[selector] = tuple(
                call.name
                for block in context.current_traj
                if block.role == 'assistant'
                for call in block.tool_calls
            )
        for agent_id, reason in tracer.stop_reasons.items():
            if agent_id in self._bound and reason is not None:
                stop_reasons[self._selector(agent_id)] = reason
        if subscription.dropped:
            self.divergences.append(
                Divergence('outcome', 'events', f'事件订阅丢了 {subscription.dropped} 个事件')
            )
        workspace = {} if self._stubbed else _workspace_contents(root)
        return ScenarioResult(
            name=self._document.name,
            final_output=MappingProxyType(final_output),
            tool_order=MappingProxyType(tool_order),
            stop_reasons=MappingProxyType(stop_reasons),
            errors=MappingProxyType(dict(self._errors)),
            statuses=MappingProxyType(statuses),
            events=tuple(events),
            workspace=MappingProxyType(workspace),
        )

    # --- comparisons ------------------------------------------------------------------------

    def _compare_gates(self, tracer: _ReplayTracer) -> None:
        recorded_any = any(
            instance.gate for role in self._document.roles.values() for instance in role.instances
        )
        # 捕获的文档每次闸门判定都记：一条都没有也要比，否则丢掉全部闸门记录的 recorder 照样
        # 「复现」。
        if not recorded_any and self._document.origin is None:
            return
        names = ('rule_ids', 'risk', 'provenance', 'decided_by', 'disposition', 'outcome')
        expected: dict[str, tuple[str, str]] = {}
        for agent_id, bound in self._bound.items():
            for gate in bound.instance.gate:
                span = span_id_of(SpanKind.TOOL_CALL, agent_id, *gate.call)
                where = f'{self._selector(agent_id)} {list(gate.call)!r}'
                expected[span] = (agent_id, where)
                observed = tracer.gates.get(span)
                if observed is None:
                    self.divergences.append(
                        Divergence('gate', where, '记录里的这次闸门判定在回放里没有发生')
                    )
                    continue
                attributes = observed[2]
                want = (
                    list(gate.rule_ids),
                    gate.risk,
                    gate.provenance,
                    gate.decided_by,
                    gate.disposition,
                    gate.outcome,
                )
                got = (
                    list(cast(list[str], attributes.get('danger.rule_ids', []))),
                    attributes.get('danger.risk'),
                    attributes.get('danger.provenance'),
                    attributes.get('danger.decided_by'),
                    attributes.get('danger.disposition'),
                    attributes.get('tool.outcome') if gate.outcome is not None else None,
                )
                if got != want:
                    differing = [
                        f'{name}: 记录 {wanted!r}，回放 {actual!r}'
                        for name, wanted, actual in zip(names, want, got, strict=True)
                        if wanted != actual
                    ]
                    self.divergences.append(Divergence('gate', where, '；'.join(differing)))
        for span, (agent_id, position, attributes) in tracer.gates.items():
            if span not in expected and agent_id in self._bound:
                decided = '，'.join(
                    f'{name}={attributes.get(key)!r}'
                    for name, key in (
                        ('rule_ids', 'danger.rule_ids'),
                        ('risk', 'danger.risk'),
                        ('provenance', 'danger.provenance'),
                        ('decided_by', 'danger.decided_by'),
                        ('disposition', 'danger.disposition'),
                        ('outcome', 'tool.outcome'),
                    )
                )
                self.divergences.append(
                    Divergence(
                        'gate',
                        f'{self._selector(agent_id)} {position!r}',
                        f'回放里多出一次记录里没有的闸门判定：{decided}',
                    )
                )

    def _report_unconsumed(self) -> None:
        self.divergences.extend(self._leftovers())
        if self._pending:
            self.divergences.append(
                Divergence(
                    'unconsumed',
                    'host_actions',
                    f'{[action.kind for action in self._pending]!r} 没有发生',
                )
            )
        for key in self._ids:
            if key not in self._used_ids:
                self.divergences.append(
                    Divergence('unconsumed', f'ids {list(key)!r}', '这个稳定键在回放里没有交出 id')
                )

    def _compare_expect(self, result: ScenarioResult) -> None:
        expected = self._document.expect
        pairs: tuple[tuple[str, object, object], ...] = (
            ('final_output', dict(expected.final_output), dict(result.final_output)),
            ('tool_order', dict(expected.tool_order), dict(result.tool_order)),
            ('stop_reasons', dict(expected.stop_reasons), dict(result.stop_reasons)),
            ('errors', dict(expected.errors), dict(result.errors)),
            ('statuses', dict(expected.statuses), dict(result.statuses)),
            ('workspace', dict(expected.workspace), dict(result.workspace)),
        )
        for name, wanted, got in pairs:
            if name in expected.specified and wanted != got:
                self.divergences.append(
                    Divergence('outcome', f'expect.{name}', f'记录 {wanted!r}，回放 {got!r}')
                )
        if 'events' not in expected.specified:
            return
        by_agent = expected.events_by_agent
        if by_agent is None:
            kinds = tuple(event.kind for event in result.events)
            if expected.events != kinds:
                self.divergences.append(
                    Divergence(
                        'outcome', 'expect.events', f'记录 {expected.events!r}，回放 {kinds!r}'
                    )
                )
            return
        for selector, wanted_kinds in by_agent.items():
            got_kinds = tuple(event.kind for event in result.events if event.agent == selector)
            if wanted_kinds != got_kinds:
                self.divergences.append(
                    Divergence(
                        'outcome',
                        f'expect.events.{selector}',
                        f'记录 {wanted_kinds!r}，回放 {got_kinds!r}',
                    )
                )


class _ReplayIds:
    """按稳定键交回记录下的 id。"""

    def __init__(self, replay: _Replay) -> None:
        self._replay = replay

    def agent_id(self, key: StableKey) -> str:
        return self._replay.issue(key, 'agent')

    def message_id(self, key: StableKey) -> str:
        return self._replay.issue(key, 'message')


class _ReplayLLM:
    """所有角色共用的回放模型：按「当前 LLM 调用」的键取记录。"""

    def __init__(self, replay: _Replay) -> None:
        self._replay = replay
        self._config = LLMConfig(provider='replay', model='replay')

    @property
    def config(self) -> LLMConfig:
        return self._config

    async def create(self, messages: list[ContextBlock], **kwargs: Any) -> ContextBlock:
        key = current_llm_call()
        if key is None:
            raise ConfigError(
                expected='回放模型只在内核的一次模型调用里被调用（「当前 LLM 调用」有值）',
                received=(
                    '这次调用不在内核的计数里：模型调用前后的 hook、工具或 Module 自己发起的调用'
                ),
                next_action='回放按（Agent、轮、第几次）取记录，取不到键就交不出属于这次调用的回复；'
                '这类调用在回放里不支持',
            )
        agent_id, turn, attempt = key
        return await self._replay.reply(agent_id, turn, attempt, {'messages': messages, **kwargs})


class _RecordedTool(Tool):
    """一个触达外部世界的工具的回放替身：名字、schema、并发性与要执行的参数都照真的那个，执行时按
    调用位置交回记录的结局。"""

    def __init__(self, real: Tool, replay: _Replay) -> None:
        self._real = real
        self._replay = replay

    @property
    def name(self) -> str:
        return self._real.name

    @property
    def schema(self) -> dict[str, Any]:
        return self._real.schema

    def is_concurrency_safe(self, args: dict[str, Any]) -> bool:
        return self._real.is_concurrency_safe(args)

    async def execute(self, **kwargs: Any) -> ToolResult:
        return await self._replay.outcome(self.name, kwargs)


def _stub(tool: Tool, replay: _Replay) -> Tool:
    if tool.name not in REPLAYED_TOOLS:
        return tool
    # 类名与要执行的参数照真的那个：危险判定的说明文字里有类名，它会进轨迹与之后的请求。
    real_type = type(tool)
    stand_in = type(
        real_type.__name__,
        (_RecordedTool,),
        {
            '__module__': real_type.__module__,
            '__qualname__': real_type.__qualname__,
            '__tool_command_args__': real_type.__tool_command_args__,
        },
    )
    return cast(Tool, stand_in(tool, replay))


def _rule(spec: DangerRuleSpec) -> DangerRule:
    by_id = {rule.rule_id: rule for rule in DEFAULT_RULES}
    if spec.id in by_id:
        return by_id[spec.id]
    if spec.id == 'shell.git_state_change':
        return git_state_change_rule()
    factory = (
        path_outside_workspace_rule
        if spec.id == 'shell.path_outside_workspace'
        else workspace_root_removal_rule
    )
    return factory(spec.params['root'], real=spec.params.get('real'))


async def _begun(recorder: TraceRecorder | None) -> TraceRecorder | None:
    """开始记录回放本身；开不了头（store 写不进去）只少这一份记录。"""
    if recorder is None:
        return None
    try:
        await recorder.begin()
    except Exception as error:
        logger.warning(
            '期望: 回放本身记成一个 trace。 实际: 开头时抛出 %s: %s；这次回放不记录，照常回放。'
            ' 下一步: 检查 trace 目录（磁盘空间、权限）。',
            type(error).__name__,
            error,
        )
        return None
    return recorder


def _known_error(qualified: str) -> type[Exception]:
    """记录下的类型名对应的内核错误类（已经导入的 `syndica.kernel` 模块里的）；不是内核错误时是
    `Exception`：内核只按自己的错误类型分支（超窗、工具的致命错误）。不按文档里的名字导入任何模块，
    也不碰内核之外的模块——导入与模块级的 `__getattr__` 都有副作用，而文档可能来自别人的仓库。"""
    module_name, _, name = qualified.rpartition('.')
    if module_name != 'syndica.kernel' and not module_name.startswith('syndica.kernel.'):
        return Exception
    module = sys.modules.get(module_name)
    found = None if module is None else getattr(module, name, None)
    if isinstance(found, type) and issubclass(found, SyndicaError):
        return found
    return Exception


def _payload_refs(document: ScenarioDocument) -> Iterator[PayloadText]:
    stack: list[object] = [document]
    while stack:
        item = stack.pop()
        if isinstance(item, PayloadText):
            yield item
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            stack.extend(getattr(item, entry.name) for entry in dataclasses.fields(item))
        elif isinstance(item, Mapping):
            stack.extend(cast(Mapping[object, object], item).values())
        elif isinstance(item, tuple | list | frozenset):
            stack.extend(cast(Iterable[object], item))


def _workspace_contents(root: Path) -> dict[str, str]:
    contents: dict[str, str] = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        try:
            contents[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
    return contents
