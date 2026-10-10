"""验收 Runtime Module（§7.5 / §7.6 / §7.7）：把 L1 / L2 / L3、Rubric 投影与反馈升级装进 Runtime。

内核只持有类型、协议与切点（§7.5「归属」）；这里把它们接起来，每个 Agent 一份：

- **Goal**：宿主给的（`goal=`）优先；没给就取根 Agent 的第一条用户消息（§7.6 决策 H10）。只有根协调
  者有 Goal——子 Agent 交回的东西由派它的那一方验收，不再各付一份伴生的钱。Goal 存进 RubricStore、按
  Agent id 分 scope（根 Agent 的 id 就是会话 id，恢复会话后沿用）。
- **注入**：cycle 起点把 Goal 块与生效 Rubric 块注入历史（进历史不进 System Prompt，§5.2）。Goal 就
  是第一条用户消息原文时不另注入——那一块已经在轨迹里，压缩也保护它。
- **投影**（`acceptance_rubrics`）：Goal 新出现或改变时、开工之前跑一次 RubricsBuilder；投影失败按空
  集合处理，本进程内对同一个 Goal 只再试一次。
- **交付**：装了本 Module 的 Agent 才有 `Deliver`（§7.4 显式提升）——不验收的 run 不为它多付一个工具
  定义。
- **L1**：每次登记产物都按处理器校验；交付物的违规在完成时零 token 打回。处理器抛出记一条 WARNING、
  记成「L1 没能跑」，不当成零违规。
- **L2 / L3**：`acceptance_output` / `acceptance_outcome` 各自开关、各自上限
  （`verifiers.acceptance_hook`）。
- **反馈**（`acceptance_feedback`）：Goal 之后人发来的消息提议成一条 `proposed` Rubric，人
  `activate` 之后才进验收（§7.7）。

伴生读工作区经 `WorkspaceRouter.reader_for`：读不刷新主 Agent 的写前提（评审 B2）。Runtime 没有路由
时伴生没有 Portal，L3 只能引轨迹里的原话、L1 不跑。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from syndica import __version__
from syndica.kernel.acceptance import Anchor, AnchorSource, Goal, RubricOrigin
from syndica.kernel.acceptance_inject import goal_block, goal_key, rubric_block, rubric_key
from syndica.kernel.artifacts import ArtifactHandlers, Violation
from syndica.kernel.context import ArtifactRef, Context, ContextBlock
from syndica.kernel.errors import ConfigError
from syndica.kernel.feedback import is_human_feedback, propose_feedback_rubric
from syndica.kernel.history_blocks import HistoryKind, has_block, kind_of
from syndica.kernel.hooks import HookResult, HooksCenter, Junction
from syndica.kernel.modules import FlagDeclaration, FlagValue, RuntimeModule
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.redaction import Redactor, RuleRedactor
from syndica.kernel.rubric_redaction import redacted_goal
from syndica.kernel.rubrics_builder import project_rubrics
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore
from syndica.kernel.tool import Tool
from syndica.kernel.tools.local import Deliver, routed_kind
from syndica.kernel.verifiers import acceptance_hook
from syndica.kernel.workspace import Portal

if TYPE_CHECKING:
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.message import Message
    from syndica.kernel.runtime import Runtime

__all__ = ['AcceptanceModule']

logger = logging.getLogger(__name__)

_HOST_GOAL_LOCATOR = 'msg:goal'
# 能写出产物的工具：只读的 Agent（`--no-write`、Explorer）不为 Deliver 多付一个工具定义。
_WRITERS = frozenset({'Write', 'Edit', 'Bash'})
_FEEDBACK_ID = re.compile(r'[^A-Za-z0-9_.:-]')


class _PortalReader:
    """按 `ref.version` 读产物：读到的内容摘要对不上就抛——文件在登记之后又被改过，校验的就不是这个
    版本了。"""

    def __init__(self, portal: Portal) -> None:
        self._portal = portal

    async def read(self, ref: ArtifactRef) -> str:
        data = await self._portal.read_bytes(ref.path)
        version = f'sha256:{hashlib.sha256(data).hexdigest()}'
        if version != ref.version:
            raise ConfigError(
                expected=f'{ref.path} 仍是登记时的版本 {ref.version}',
                received=f'它现在是 {version}',
                next_action='按它此刻的版本重新登记之后再校验',
            )
        return data.decode('utf-8', errors='replace')


class _FormatChecks:
    """一个 Agent 的 L1：每个交付物最近校验的那一版与结果（None = 处理器没能跑），以及它在哪个周期被
    动过。

    处理器按扩展名派生的 (kind, mime) 选：`Deliver` 的 kind 只是标签——由模型自选的 kind 决定交给哪个
    处理器，就能把交付物绕过严格的那个（收口评审 H3）。完成时按交付物此刻的版本复核：登记之后被命令
    改坏的、被删掉的都算在这一次里（H2）；只报这个周期动过的，前一周期留下的问题不让一句无关的回复也被
    打回。"""

    def __init__(
        self, handlers: ArtifactHandlers, portal: Portal | None, cycle: Callable[[], int]
    ) -> None:
        self._handlers = handlers
        self._portal = portal
        self._reader = None if portal is None else _PortalReader(portal)
        self._cycle = cycle
        self._deliverables: dict[str, ArtifactRef] = {}
        self._results: dict[str, tuple[str, list[Violation] | None]] = {}
        self._touched: dict[str, int] = {}

    async def on_registered(self, **kwargs: Any) -> HookResult:
        ref = cast(ArtifactRef, kwargs['artifact'])
        if ref.is_deliverable or ref.id in self._deliverables:
            self._deliverables[ref.id] = ref
            self._touched[ref.id] = self._cycle()
        if self._reader is not None:
            await self._validate(ref)
        return HookResult()

    async def _validate(self, ref: ArtifactRef) -> None:
        assert self._reader is not None
        kind, mime = routed_kind(ref.path)
        routed = replace(ref, kind=kind, mime=mime)
        try:
            violations = await self._handlers.for_(routed).validate(routed, self._reader)
        except Exception as exc:
            logger.warning(
                '期望: L1 校验 %s（%s）。 实际: 处理器抛出 %s。 下一步: 这一版记成「L1 没能跑」，'
                '不当成零违规；查这个格式的处理器。',
                ref.path,
                ref.version,
                type(exc).__name__,
            )
            self._results[ref.id] = (ref.version, None)
            return
        self._results[ref.id] = (ref.version, list(violations))

    async def report(self, cycle: object) -> tuple[list[str], list[str]]:
        problems: list[str] = []
        skipped: list[str] = []
        if self._reader is None or self._portal is None:
            return problems, skipped
        for artifact_id, ref in self._deliverables.items():
            last = self._results.get(artifact_id)
            try:
                current = await self._portal.stat(ref.path)
            except Exception:
                current = None
            version = _MISSING if current is None else current.version
            if last is None or last[0] != version:
                self._touched[artifact_id] = cast(int, cycle)
                if current is None:
                    gone = Violation(rule='missing', message=f'{ref.path} 不见了', anchor=ref.path)
                    self._results[artifact_id] = (_MISSING, [gone])
                else:
                    await self._validate(replace(ref, version=current.version))
            if self._touched.get(artifact_id) != cycle:
                continue
            found = self._results[artifact_id][1]
            if found is None:
                skipped.append(ref.path)
                continue
            problems.extend(f'{ref.path}：{v.message}（{v.rule}）' for v in found)
        return problems, skipped


_MISSING = 'missing'


def _first_task(ctx: Context) -> ContextBlock | None:
    for block in ctx.current_traj:
        if block.role == 'user' and kind_of(block) is None and isinstance(block.content, str):
            if block.content.strip():
                return block
    return None


def _locator(block: ContextBlock) -> str:
    message_id = block.extra.get('message_id')
    return f'msg:{message_id}' if isinstance(message_id, str) and message_id else 'msg:task'


class _AgentAcceptance:
    """一个 Agent 的验收接线：注入器、三个 hook 与它们共享的状态。"""

    def __init__(
        self,
        module: AcceptanceModule,
        runtime: Runtime,
        agent: BaseAgent,
        flags: Mapping[str, FlagValue],
    ) -> None:
        self._module = module
        self._runtime = runtime
        self._agent = agent
        self._scope = agent.agent_id
        self._flags = flags
        router = runtime.workspace
        self._portal: Portal | None = None if router is None else router.reader_for(agent.agent_id)
        self._timeout = float(cast(int, flags['acceptance_verifier_timeout']))
        self._max_turns = cast(int, flags['acceptance_verifier_max_turns'])
        self.formats = _FormatChecks(
            module.handlers,
            self._portal,
            lambda: agent._cycle,  # pyright: ignore[reportPrivateUsage]
        )
        self.completion = acceptance_hook(
            store=module.store,
            scope=self._scope,
            llm=agent.options.llm,
            portal=self._portal,
            output=bool(flags['acceptance_output']),
            outcome=bool(flags['acceptance_outcome']),
            output_retries=cast(int, flags['acceptance_output_retries']),
            partial_retries=cast(int, flags['acceptance_outcome_partial_retries']),
            block_retries=cast(int, flags['acceptance_outcome_block_retries']),
            max_turns=self._max_turns,
            timeout=self._timeout,
            format_check=self.formats.report,
        )

    @property
    def injects(self) -> bool:
        if self._module.goal is not None:
            return True  # 宿主给了 Goal：没开任何一层也注入它，`--goal` 不被静默忽略（收口评审 L5）
        return any(
            bool(self._flags[key])
            for key in (
                'acceptance_rubrics',
                'acceptance_output',
                'acceptance_outcome',
                'acceptance_feedback',
            )
        )

    async def inject(self, ctx: Context) -> None:
        """cycle 起点：定下 Goal（必要时投影）、把 Goal 块与生效 Rubric 块注入历史。Rubric 库或投影
        出错只记一条 WARNING、本轮不注入（验收在完成时照样 fail-open 成 unverified）。"""
        try:
            goal = await self._settle_goal(ctx)
            if goal is None:
                return
            rubrics = await self._module.store.rubrics(self._scope)
        except Exception as exc:
            logger.warning(
                '期望: 注入 %s 的 Goal 与 Rubric。 实际: %s。 下一步: 本轮不注入；完成时的验收会标 '
                'unverified，查 .syndica/rubrics。',
                self._scope,
                type(exc).__name__,
            )
            return
        task = _first_task(ctx)
        derived = task is not None and goal.intent == self._module.redactor.redact(
            cast(str, task.content)
        )
        shown = self._shown_goal(goal)
        if not derived and not has_block(ctx.current_traj, goal_key(shown)):
            ctx.append(goal_block(shown))
        stale = not rubrics and any(kind_of(b) is HistoryKind.RUBRIC for b in ctx.current_traj)
        if (rubrics or stale) and not has_block(ctx.current_traj, rubric_key(rubrics)):
            # 集合变空也追加一块替代：否则上一个 Goal 的标准仍是最新、受压缩保护的那块。
            ctx.append(rubric_block(rubrics))

    def _shown_goal(self, stored: Goal) -> Goal:
        """注入给模型看的 Goal：宿主给的那句按原文（只在落盘时脱敏，注入历史不改写模型看到的指令，
        收口评审 M3）；派生的与读回的照存下的。"""
        host = self._module.goal
        if host is None or stored.intent != self._module.redactor.redact(host):
            return stored
        return Goal(intent=host, anchors=(_host_anchor(host),), trust=stored.trust)

    async def _settle_goal(self, ctx: Context) -> Goal | None:
        store = self._module.store
        stored = await store.get_goal(self._scope)
        wanted = await self._wanted_goal(ctx, stored)
        if wanted is not None:
            await store.put_goal(self._scope, redacted_goal(wanted, self._module.redactor))
            stored = await store.get_goal(self._scope)
        if stored is None:
            return None
        if self._flags['acceptance_rubrics'] and await self._needs_projection(stored, wanted):
            sources = {anchor.locator: anchor.quote for anchor in stored.anchors}
            task = _first_task(ctx)
            if task is not None:
                # 投影「从 Goal 与任务输入」推导（§7.6）：宿主给了 Goal 时，任务原文也要锚得回去。
                sources.setdefault(
                    _locator(task), self._module.redactor.redact(cast(str, task.content))
                )
            await project_rubrics(
                scope=self._scope,
                goal=stored,
                sources=sources,
                llm=self._agent.options.llm,
                store=store,
                redactor=self._module.redactor,
                trust=stored.trust,
                max_turns=self._max_turns,
                time_limit=self._timeout,
            )
        return stored

    async def _wanted_goal(self, ctx: Context, stored: Goal | None) -> Goal | None:
        """这次要写进去的 Goal；不用写时交回 None。宿主给的 Goal 与已存的不同就换（人明确改了要求，
        §7.6）；没给就只在还没有 Goal 时从第一条用户消息派生。"""
        if not await self._runtime._is_root_coordinator(self._scope):  # pyright: ignore[reportPrivateUsage]
            return None
        host = self._module.goal
        if host is not None:
            if stored is not None and stored.intent == self._module.redactor.redact(host):
                return None
            return Goal(intent=host, anchors=(_host_anchor(host),))
        if stored is not None:
            return None
        task = _first_task(ctx)
        if task is None:
            return None
        text = cast(str, task.content)
        anchor = Anchor(AnchorSource.USER_MESSAGE, _locator(task), text)
        return Goal(intent=text, anchors=(anchor,), trust=task.trust)

    async def _needs_projection(self, goal: Goal, wanted: Goal | None) -> bool:
        key = (self._scope, goal_key(goal))
        if wanted is None and key in self._module.attempted:
            return False
        if wanted is None:
            existing = await self._module.store.rubrics(self._scope, include_proposed=True)
            if any(rubric.origin is RubricOrigin.PROJECTED for rubric in existing):
                return False
        self._module.attempted.add(key)
        return True

    async def on_message(self, **kwargs: Any) -> HookResult:
        """Goal 之后人发来的消息提议成一条 `proposed` Rubric（§7.7）。Agent 发的、定下 Goal 的那条、
        没有 Goal 时都不提议。提议出错只记一条 WARNING。"""
        message = cast('Message', kwargs['message'])
        content = message.content
        if not content.strip():
            return HookResult()
        try:
            goal = await self._module.store.get_goal(self._scope)
            if goal is None:
                return HookResult()
            locator = f'msg:{message.id}'
            if any(anchor.locator == locator for anchor in goal.anchors):
                return HookResult()
            agents = {info.agent_id for info in await self._runtime.registry.list()}
            if not is_human_feedback(message.sender, agents | self._module.known_agents):
                return HookResult()
            async with asyncio.timeout(self._timeout):
                await self._propose(message, locator, content)
        except Exception as exc:
            logger.warning(
                '期望: 把人的反馈提议成 Rubric。 实际: %s（%s）。 '
                '下一步: 这条反馈没有提议；它仍作为普通消息进了对话。',
                self._scope,
                type(exc).__name__,
            )
        return HookResult()

    async def _propose(self, message: Message, locator: str, content: str) -> None:
        """有时限（同验收器）：提议在 MESSAGE_INJECTED 里同步跑，挂住就挂住主 Agent 这一轮（收口评审
        M2）。"""
        await propose_feedback_rubric(
            scope=self._scope,
            rubric_id=_feedback_id(message.id),
            feedback=content,
            locator=locator,
            store=self._module.store,
            redactor=self._module.redactor,
            llm=self._agent.options.llm,
            trust=message.trust,
            max_turns=self._max_turns,
        )


def _host_anchor(host: str) -> Anchor:
    return Anchor(AnchorSource.USER_MESSAGE, _HOST_GOAL_LOCATOR, host)


def _feedback_id(message_id: str) -> str:
    return 'fb:' + _FEEDBACK_ID.sub('-', message_id)[:61]


class _AcceptancePlugin:
    def __init__(
        self, hooks: Sequence[tuple[Junction, Callable[..., Awaitable[HookResult]]]]
    ) -> None:
        self._hooks = tuple(hooks)

    @property
    def name(self) -> str:
        return 'acceptance'

    def register(self, hooks: HooksCenter) -> None:
        for junction, hook in self._hooks:
            hooks.register(junction, hook)


class AcceptanceModule(RuntimeModule):
    """把产物验收接进 Runtime（spec §7.5–§7.7）。

    `store` 缺省用内存 RubricStore（宿主要跨会话就传文件实现）；`goal` 是宿主给的 Goal（CLI 的
    `--goal`），不给就从根 Agent 的第一条用户消息派生；`handlers` 是 L1 的处理器注册表（缺省只有
    `generic`）。
    """

    def __init__(
        self,
        *,
        store: RubricStore | None = None,
        redactor: Redactor | None = None,
        goal: str | None = None,
        handlers: ArtifactHandlers | None = None,
    ) -> None:
        if goal is not None and (not isinstance(cast(object, goal), str) or not goal.strip()):
            raise ConfigError(
                expected='goal 是一句非空的要求，或省略（从第一条用户消息派生）',
                received=f'goal={goal!r}',
                next_action='写清这次要交付什么，或去掉 goal',
            )
        self.store: RubricStore = store if store is not None else InMemoryRubricStore()
        self.redactor: Redactor = redactor if redactor is not None else RuleRedactor()
        self.goal = goal
        self.handlers = handlers if handlers is not None else ArtifactHandlers()
        self.attempted: set[tuple[str, str]] = set()
        # 这个 Runtime 里起过的每个 Agent：退出之后它发过的消息仍不是人的反馈（收口评审 M1）。
        self.known_agents: set[str] = set()

        def plugin_factory(
            runtime: Runtime, agent: BaseAgent, flags: Mapping[str, FlagValue]
        ) -> _AcceptancePlugin:
            self.known_agents.add(agent.agent_id)
            wiring = _AgentAcceptance(self, runtime, agent, flags)
            hooks: list[tuple[Junction, Callable[..., Awaitable[HookResult]]]] = [
                (Junction.ARTIFACT_REGISTERED, wiring.formats.on_registered),
                (Junction.COMPLETION_PROPOSED, wiring.completion),
            ]
            if flags['acceptance_feedback']:
                hooks.append((Junction.MESSAGE_INJECTED, wiring.on_message))
            if wiring.injects:
                agent._attach_rubrics(wiring.inject)  # pyright: ignore[reportPrivateUsage]
            return _AcceptancePlugin(hooks)

        def tool_factory(
            runtime: Runtime, agent: BaseAgent, flags: Mapping[str, FlagValue]
        ) -> Mapping[str, Tool]:
            router = runtime.workspace
            tools = agent.options.tools
            if router is None or 'Deliver' in tools or not _WRITERS & set(tools):
                return {}
            # 收的类别跟着同一份注册表走：Module 装上的处理器声明的类别，这里不收就等于没装。
            return {'Deliver': Deliver(router.portal(), kinds=self.handlers.kinds)}

        super().__init__(
            'acceptance',
            version=__version__,
            description='产物验收：L1 格式 / L2 输出 / L3 结果，Rubric 投影与反馈升级（§7.5–§7.7）',
            capabilities=(),
            replay_safe=False,
            flags=(
                FlagDeclaration(
                    key='acceptance_rubrics',
                    default=False,
                    description='开工前把 Goal 投影成带锚点的 Rubric（一次伴生调用）',
                ),
                FlagDeclaration(
                    key='acceptance_output',
                    default=False,
                    description='L2 输出验收：完成时判断用户的要求是否都已做完（伴生，判定保守）',
                ),
                FlagDeclaration(
                    key='acceptance_output_retries',
                    default=1,
                    description='L2 判没做完或交付物没过 L1 时最多打回几次；0 = 只记裁决',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='acceptance_outcome',
                    default=False,
                    description='L3 结果验收：完成时按 Rubric 逐条查回复与工作区证据（伴生）',
                ),
                FlagDeclaration(
                    key='acceptance_outcome_block_retries',
                    default=2,
                    description='L3 判 block（有证据表明做错了）时最多打回几次；0 = 只记裁决',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='acceptance_outcome_partial_retries',
                    default=1,
                    description='L3 判 partial（没做完或没能确认）时最多打回几次；0 = 只记裁决',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='acceptance_feedback',
                    default=False,
                    description='把 Goal 之后人发来的消息提议成 proposed Rubric，人确认后才生效',
                ),
                FlagDeclaration(
                    key='acceptance_verifier_timeout',
                    default=300,
                    description='一次验收（L2 + L3 合计）或投影的时限，秒；超时放行并标 unverified',
                    minimum=1,
                ),
                FlagDeclaration(
                    key='acceptance_verifier_max_turns',
                    default=8,
                    description='每个伴生 Agent（投影、L2、L3、反馈提议）各自的轮次上限',
                    minimum=1,
                ),
            ),
            agent_plugins=(plugin_factory,),
            tool_factories=(tool_factory,),
        )
