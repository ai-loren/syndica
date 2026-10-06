"""长程 Module（spec §8.2–§8.7）：进度账本、停滞、漂移、预算低，交给可插拔的策略决定怎么纠偏。

只作用于有 Goal 的 Agent（验收 Module 只给根协调者定 Goal）；进度只来自验收裁决（完成时的 L3，加上每
`long_horizon_progress_every` 轮一次的里程碑 L3），所以依赖验收 Module 并要求 `acceptance_outcome`
开着。

策略交回的文字由内核署名追加，信任取下限与主轨迹溯源里最严的：下限只对内核的 `BaselineStrategy` 是可
信，别的策略缺省不可信（第三方策略与第三方 Skill 一样，「宿主装了」不等于「宿主读过每一行」），宿主
用 `trusted_strategy=True` 声明可信。策略抛出、挂住或交回不认识的东西都失败开放（这一处不纠偏）并记
ERROR；取消照常上抛。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from syndica import __version__
from syndica.kernel.acceptance import Goal, Rubric, RubricKind, RubricStatus, Verdict, VerdictStatus
from syndica.kernel.companion import run_companion
from syndica.kernel.context import (
    Context,
    ContextBlock,
    TrustLevel,
    provenance_of,
    strictest_trust,
)
from syndica.kernel.drift import action_terms, anchor_terms, anchored
from syndica.kernel.errors import ConfigError
from syndica.kernel.history_blocks import HistoryKind
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.long_horizon import (
    BaselineStrategy,
    BudgetAction,
    BudgetMove,
    DriftAction,
    DriftMove,
    DriftReport,
    LongHorizonContext,
    LongHorizonStrategy,
    ProgressSnapshot,
    StallAction,
    StallMove,
    StopDecision,
)
from syndica.kernel.long_horizon_seam import LongHorizonBlock
from syndica.kernel.modules import FlagDeclaration, FlagValue, RuntimeModule
from syndica.kernel.progress import ProgressLedger, ProgressRecord
from syndica.kernel.verifiers import (
    _evidence_corpus,  # pyright: ignore[reportPrivateUsage]
    render_transcript,
    verify_outcome,
)

if TYPE_CHECKING:
    from syndica.kernel.acceptance_module import AcceptanceModule
    from syndica.kernel.agent import BaseAgent
    from syndica.kernel.runtime import Runtime

__all__ = ['LongHorizonModule']

logger = logging.getLogger(__name__)

# 一次策略调用的上限：策略在每轮模型调用之前被问，挂住就挂住这一轮。
_STRATEGY_TIMEOUT = 30.0
# 策略交回的一段话进历史的上限：它每次都进前缀，一段失控的长文本每轮都要付钱。
_MESSAGE_CHARS = 2000
_ACTIONS: Mapping[str, type] = {
    'on_progress_stall': StallAction,
    'on_budget_low': BudgetAction,
    'on_goal_drift': DriftAction,
    'should_stop': StopDecision,
}
_MOVES: Mapping[str, type] = {
    'on_progress_stall': StallMove,
    'on_budget_low': BudgetMove,
    'on_goal_drift': DriftMove,
}
_DRIFT_PROMPT = (
    '你判断一个 Agent 最近的动作是否还在为给定的目标干活。'
    '只看目标与动作，不要做任何别的事，不要调用工具。'
    '只回一个 JSON 对象：{"on_track": true 或 false, "reason": "一句话"}。'
)


@dataclass(frozen=True)
class _TurnActions:
    """一轮里的动作：词面单位、交给语义比对的那几行、以及它们来自的块里最严的信任。"""

    terms: frozenset[str] = frozenset()
    lines: tuple[str, ...] = ()
    trust: TrustLevel = TrustLevel.TRUSTED


def _last_batch(blocks: Sequence[ContextBlock]) -> tuple[ContextBlock, ...]:
    """轨迹末尾最近的一批工具调用与它们的结果（跳过此后注入的消息与署名块）；最近一次回复没有调用
    工具就是空。从末尾往回找而不是记下标：压缩会挪动下标，而它总留着最近的轮次。"""
    results: list[ContextBlock] = []
    for block in reversed(blocks):
        if block.role == 'tool':
            results.append(block)
        elif block.role == 'assistant':
            return (block, *reversed(results)) if block.tool_calls else ()
    return ()


class _NoHooks:
    @property
    def name(self) -> str:
        return 'long-horizon'

    def register(self, hooks: HooksCenter) -> None:
        return None


class _Controller:
    """一个 Agent 的长程接线：账本、停滞 / 漂移 / 预算低的判据，以及向策略要动作。"""

    def __init__(
        self,
        module: LongHorizonModule,
        runtime: Runtime,
        agent: BaseAgent,
        flags: Mapping[str, FlagValue],
    ) -> None:
        self._module = module
        self._agent = agent
        self._scope = agent.agent_id
        router = runtime.workspace
        self._portal = None if router is None else router.reader_for(agent.agent_id)
        self._every = cast(int, flags['long_horizon_progress_every'])
        self._stall = cast(int, flags['long_horizon_stall_turns'])
        self._low = cast(int, flags['long_horizon_budget_low_percent'])
        self._window = cast(int, flags['long_horizon_drift_window'])
        self._semantic = bool(flags['long_horizon_semantic_drift'])
        self._max_continuations = cast(int, flags['long_horizon_max_continuations'])
        self._verifier_turns = cast(int, flags['acceptance_verifier_max_turns'])
        self._verifier_timeout = float(cast(int, flags['acceptance_verifier_timeout']))
        self._ledger = ProgressLedger()
        self._verdict: Verdict | None = None
        self._recent: deque[_TurnActions] = deque(maxlen=max(self._window, 1))
        self._counted: tuple[str, ...] = ()
        self._asked_before = False
        self._last_stall: int | None = None
        self._last_drift = 0
        self._drift: DriftReport | None = None
        self._low_generation: int | None = None
        self._wrap_cycle: int | None = None
        self._final = False
        self._final_offered = False
        self._cycle = 0
        self._continuations = 0
        self._warned_fallback = False

    # ---- 内核问的几处 ----

    def wrap_up_requested(self) -> bool:
        """策略要的收尾只对提出它的那个周期有效：那个周期没再开下一轮就结束了，被叫醒的下一个周期
        不能一上来就收尾。"""
        wrap_cycle, self._wrap_cycle = self._wrap_cycle, None
        return wrap_cycle is not None and wrap_cycle == self._agent._cycle  # pyright: ignore[reportPrivateUsage]

    def final_requested(self) -> bool:
        final, self._final = self._final, False
        return final

    async def before_model(self, ctx: Context, turn: int) -> Sequence[LongHorizonBlock]:
        cycle = self._agent._cycle  # pyright: ignore[reportPrivateUsage]
        if cycle != self._cycle:
            self._cycle = cycle
            self._continuations = 0
            self._final_offered = False
        actions = self._actions_since_last_turn(ctx)
        loaded = await self._load()
        if loaded is None:
            return ()
        goal, rubrics = loaded
        blocks: list[LongHorizonBlock] = []
        if self._every and turn > 1 and (turn - 1) % self._every == 0:
            verdict = await self._milestone(ctx, rubrics)
            if verdict is not None:
                await self._record(verdict, rubrics, turn)
        await self._stall_check(ctx, turn, goal, rubrics, blocks)
        await self._budget_check(ctx, turn, goal, rubrics, blocks)
        await self._drift_check(ctx, turn, goal, rubrics, actions, blocks)
        await self._stop_check(ctx, turn, goal, rubrics, blocks)
        return blocks

    def _actions_since_last_turn(self, ctx: Context) -> _TurnActions:
        """上一轮做了什么。同一批调用只算一次：上一轮的模型调用没交回东西时，末尾那批还是更早那一轮的。"""
        batch = _last_batch(ctx.current_traj)
        ids = tuple(call.id for call in batch[0].tool_calls) if batch else ()
        if not ids or ids == self._counted:
            return _TurnActions()
        self._counted = ids
        lines = tuple(f'- {call.name} {call.arguments}' for call in batch[0].tool_calls)
        return _TurnActions(action_terms(batch), lines, _strictest(block.trust for block in batch))

    async def after_completion(self, ctx: Context, turn: int) -> LongHorizonBlock | None:
        verdict = ctx.verdict
        if verdict is None:
            return None
        loaded = await self._load()
        if loaded is None:
            return None
        goal, rubrics = loaded
        await self._record(verdict, rubrics, turn)
        # 「接着干」只在验收打回用完之后（走到这里说明没被打回）、只对 partial / block：unverified
        # 是验收器没能判，接着干只是在一个坏掉的验收器上烧预算。
        if verdict.status not in (VerdictStatus.PARTIAL, VerdictStatus.BLOCK):
            return None
        if self._continuations >= self._max_continuations:
            return None
        decision = await self._ask('should_stop', self._context(ctx, turn, goal, rubrics))
        if not isinstance(decision, StopDecision) or decision.stop or not decision.reason.strip():
            return None
        self._continuations += 1
        return LongHorizonBlock(
            HistoryKind.STRATEGY,
            _clipped(decision.reason),
            key=f'strategy:{self._scope}:continue:{self._cycle}:{turn}',
            trust=strictest_trust(self._module.floor, verdict.trust),
        )

    # ---- 判据 ----

    async def _load(self) -> tuple[Goal, tuple[Rubric, ...]] | None:
        store = self._module.acceptance.store
        try:
            goal = await store.get_goal(self._scope)
            if goal is None:
                return None
            rubrics = tuple(await store.rubrics(self._scope))
        except Exception as exc:
            logger.warning(
                '期望: 读到 %s 的 Goal 与 Rubric。 实际: %s。 下一步: 这一轮不做长程判定；'
                '查 .syndica/rubrics。',
                self._scope,
                type(exc).__name__,
            )
            return None
        return goal, rubrics

    async def _milestone(self, ctx: Context, rubrics: Sequence[Rubric]) -> Verdict | None:
        """里程碑 L3（spec §8.7）：按 Rubric 逐条查一次此刻的证据。没有生效的 checkable 条就不
        量。"""
        if not any(
            r.status is RubricStatus.ACTIVE and r.kind is RubricKind.CHECKABLE for r in rubrics
        ):
            return None
        try:
            return await verify_outcome(
                llm=self._agent.options.llm,
                rubrics=rubrics,
                transcript=render_transcript(ctx),
                portal=self._portal,
                max_turns=self._verifier_turns,
                time_limit=self._verifier_timeout,
                trust=provenance_of(ctx),
                evidence_text=_evidence_corpus(ctx),
            )
        except Exception as exc:
            logger.warning(
                '期望: 里程碑验收给出结论。 实际: %s。 下一步: 这一次不量进度；反复出现就查验收用的'
                '模型。',
                type(exc).__name__,
            )
            return None

    async def _record(
        self, verdict: Verdict, rubrics: Sequence[Rubric], turn: int
    ) -> ProgressRecord:
        self._verdict = verdict
        record = self._ledger.record(verdict, rubrics, turn)
        snapshot = record.snapshot
        if record.fallback and not self._warned_fallback:
            self._warned_fallback = True
            logger.warning(
                '期望: agent %s 有强制的 checkable 验收条可以量进度。'
                ' 实际: 一条都没有，进度改按全部生效的 checkable 条算。'
                ' 下一步: 在 .syndica/rubrics 里把关键的条标成 mandatory。',
                self._scope,
            )
        self._event(
            'progress.measured',
            {
                'progress.satisfied': len(snapshot.satisfied),
                'progress.mandatory': len(snapshot.mandatory),
                'progress.pending_human': len(snapshot.pending_human),
            },
        )
        if record.regressed:
            # 回退是警报（spec §8.3）：此前满足的强制条现在被判 UNMET。
            logger.warning(
                '期望: 已满足的验收条保持满足。 实际: agent %s 的 %s 此前满足、这次被判 UNMET。'
                ' 下一步: 看 trace 里这一轮前后的改动。',
                self._scope,
                list(record.regressed),
            )
            self._event('progress.regressed', {'progress.regressed': list(record.regressed)})
            await self._emit('progress_regressed', {'rubrics': list(record.regressed)})
        return record

    async def _stall_check(
        self,
        ctx: Context,
        turn: int,
        goal: Goal,
        rubrics: Sequence[Rubric],
        blocks: list[LongHorizonBlock],
    ) -> None:
        if not self._stall:
            return
        stalled = self._ledger.stalled_turns(turn)
        if stalled is None or stalled < self._stall:
            return
        # 边沿触发：触发之后要再满一个窗口才再触发，不是此后每一轮都触发。
        if self._last_stall is not None and turn - self._last_stall < self._stall:
            return
        self._last_stall = turn
        self._event('progress.stalled', {'progress.stalled_turns': stalled})
        await self._emit('progress_stalled', {'turns': stalled})
        action = await self._ask('on_progress_stall', self._context(ctx, turn, goal, rubrics))
        if isinstance(action, StallAction) and action.move is StallMove.NUDGE and action.message:
            blocks.append(
                LongHorizonBlock(
                    HistoryKind.STRATEGY,
                    _clipped(action.message),
                    key=f'strategy:{self._scope}:stall:{turn}',
                    trust=self._module.floor,
                )
            )

    async def _budget_check(
        self,
        ctx: Context,
        turn: int,
        goal: Goal,
        rubrics: Sequence[Rubric],
        blocks: list[LongHorizonBlock],
    ) -> None:
        budget = self._agent._budget  # pyright: ignore[reportPrivateUsage]
        if not self._low or budget is None or budget.companion or not budget.limited:
            return
        fraction = budget.fraction_left()
        if fraction is None or fraction * 100 > self._low:
            return
        if self._low_generation == budget.generation:
            return
        self._low_generation = budget.generation
        action = await self._ask('on_budget_low', self._context(ctx, turn, goal, rubrics))
        if not isinstance(action, BudgetAction):
            return
        if action.move is BudgetMove.WRAP_UP:
            self._wrap_cycle = self._cycle
        if action.message:
            blocks.append(
                LongHorizonBlock(
                    HistoryKind.STRATEGY,
                    _clipped(action.message),
                    key=f'strategy:{self._scope}:budget:{budget.generation}',
                    trust=self._module.floor,
                )
            )

    async def _drift_check(
        self,
        ctx: Context,
        turn: int,
        goal: Goal,
        rubrics: Sequence[Rubric],
        actions: _TurnActions,
        blocks: list[LongHorizonBlock],
    ) -> None:
        if not self._window:
            return
        # 每次问到时记的是上一轮的动作：第一次被问时还没有上一轮。
        if self._asked_before:
            self._recent.append(actions)
        self._asked_before = True
        if len(self._recent) < self._window or turn - self._last_drift < self._window:
            return
        self._last_drift = turn
        window = tuple(self._recent)
        done = frozenset[str]().union(*(entry.terms for entry in window))
        if anchored(done, anchor_terms(goal, rubrics)):
            self._drift = DriftReport(self._window, anchored=True)
            return
        confirmed, reason = None, ''
        if self._semantic:
            confirmed, reason = await self._semantic_check(goal, window)
        self._drift = DriftReport(self._window, anchored=False, confirmed=confirmed, reason=reason)
        self._event('goal.drift', {'drift.anchored': False, 'drift.confirmed': confirmed})
        await self._emit('goal_drift', {'confirmed': confirmed})
        if confirmed is False:
            return
        action = await self._ask('on_goal_drift', self._context(ctx, turn, goal, rubrics))
        if isinstance(action, DriftAction) and action.move is DriftMove.REMIND:
            # 重申的是 Goal 原文，信任随 Goal：复述出来的是「用户要什么」的第二个版本（P1 L8）。
            text = f'{_clipped(action.message)}\n{goal.intent}' if action.message else goal.intent
            blocks.append(
                LongHorizonBlock(
                    HistoryKind.STRATEGY,
                    text,
                    key=f'strategy:{self._scope}:drift:{turn}',
                    trust=strictest_trust(self._module.floor, goal.trust),
                )
            )

    async def _semantic_check(
        self, goal: Goal, window: Sequence[_TurnActions]
    ) -> tuple[bool | None, str]:
        """第二层：一次伴生比对。失败开放成「不确认漂移」（None）；结论的信任随伴生读到的东西。"""
        actions = '\n'.join(line for entry in window for line in entry.lines)
        task = f'目标：{goal.intent}\n\n最近的动作：\n{actions or "（没有工具调用）"}'
        trust = strictest_trust(goal.trust, _strictest(entry.trust for entry in window))
        try:
            async with asyncio.timeout(self._verifier_timeout):
                result = await run_companion(
                    name='DriftJudge',
                    llm=self._agent.options.llm,
                    system_prompt=_DRIFT_PROMPT,
                    task=task,
                    max_turns=2,
                    trust=trust,
                )
            text = result.reply.strip()
            data = cast(object, json.loads(text[text.find('{') : text.rfind('}') + 1]))
        except Exception as exc:
            logger.warning(
                '期望: 漂移的语义比对给出结论。 实际: %s。 下一步: 这一次不确认漂移；'
                '反复出现就关掉 long_horizon_semantic_drift 或换模型。',
                type(exc).__name__,
            )
            return None, ''
        if not isinstance(data, dict):
            return None, ''
        verdict = cast(dict[str, object], data)
        on_track = verdict.get('on_track')
        reason = verdict.get('reason')
        if not isinstance(on_track, bool):
            return None, ''
        return (not on_track), (reason if isinstance(reason, str) else '')

    async def _stop_check(
        self,
        ctx: Context,
        turn: int,
        goal: Goal,
        rubrics: Sequence[Rubric],
        blocks: list[LongHorizonBlock],
    ) -> None:
        """跑到一半的里程碑判了 pass、强制条也都满足：问策略要不要停。要停就安排一轮最终回答，而不是
        立刻停——此刻最后一块可能还是一批工具结果，立刻停下就没有回答。每个周期只提一次。"""
        latest = self._ledger.latest
        verdict = self._verdict
        if self._final_offered or latest is None or not latest.complete:
            return
        if verdict is None or verdict.status is not VerdictStatus.PASS:
            return
        decision = await self._ask('should_stop', self._context(ctx, turn, goal, rubrics))
        if not isinstance(decision, StopDecision) or not decision.stop:
            return
        self._final_offered = True
        self._final = True
        blocks.append(
            LongHorizonBlock(
                HistoryKind.STRATEGY,
                '强制验收条都已满足：这一轮直接给出最终回答，交代交付了什么；不要再调用工具。',
                key=f'strategy:{self._scope}:final:{turn}',
                trust=strictest_trust(self._module.floor, verdict.trust),
            )
        )

    # ---- 策略 ----

    def _context(
        self, ctx: Context, turn: int, goal: Goal, rubrics: Sequence[Rubric]
    ) -> LongHorizonContext:
        budget = self._agent._budget  # pyright: ignore[reportPrivateUsage]
        stalled = self._ledger.stalled_turns(turn)
        return LongHorizonContext(
            agent_id=self._scope,
            cycle=self._cycle,
            turn=turn,
            goal=goal,
            rubrics=tuple(rubrics),
            progress=self._ledger.latest or ProgressSnapshot(),
            budget=None if budget is None else budget.view(),
            stalled_turns=0 if stalled is None else stalled,
            drift=self._drift,
            verdict=self._verdict,
        )

    async def _ask(self, hook: str, snapshot: LongHorizonContext) -> object:
        strategy = self._module.strategy
        expected = _ACTIONS[hook]
        try:
            async with asyncio.timeout(_STRATEGY_TIMEOUT):
                action = await getattr(strategy, hook)(snapshot)
        except TimeoutError:
            _report_strategy(strategy, hook, f'{_STRATEGY_TIMEOUT} 秒内没有交回')
            return None
        except Exception as exc:
            _report_strategy(strategy, hook, f'抛出 {type(exc).__name__}: {exc}')
            return None
        if not isinstance(cast(object, action), expected):
            _report_strategy(
                strategy, hook, f'交回了 {type(action).__name__}，不是 {expected.__name__}'
            )
            return None
        problem = _malformed(hook, action)
        if problem is not None:
            _report_strategy(strategy, hook, problem)
            return None
        move = getattr(action, 'move', None)
        value = (
            ('stop' if action.stop else 'continue')
            if isinstance(action, StopDecision)
            else getattr(move, 'value', 'continue')
        )
        self._event('strategy.action', {'strategy.hook': hook, 'strategy.move': value})
        return action

    def _event(self, name: str, attributes: dict[str, Any]) -> None:
        self._agent._tracing.event(name, attributes)  # pyright: ignore[reportPrivateUsage]

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        await self._agent._emit(kind, payload)  # pyright: ignore[reportPrivateUsage]


def _strictest(levels: Iterable[TrustLevel]) -> TrustLevel:
    trust = TrustLevel.TRUSTED
    for level in levels:
        trust = strictest_trust(trust, level)
    return trust


def _malformed(hook: str, action: object) -> str | None:
    """动作的字段对不对：dataclass 不校验类型，一个别的枚举的成员或一个字符串都能塞进 move。"""
    if isinstance(action, StopDecision):
        if not isinstance(cast(object, action.stop), bool):
            return f'交回的 stop 是 {type(action.stop).__name__}，不是 bool'
        text = cast(object, action.reason)
    else:
        move = cast(object, getattr(action, 'move', None))
        if not isinstance(move, _MOVES[hook]):
            return f'交回的 move 是 {move!r}，不是 {_MOVES[hook].__name__} 的成员'
        text = cast(object, getattr(action, 'message', None))
    if not isinstance(text, str):
        return f'交回的文字是 {type(text).__name__}，不是 str'
    return None


def _clipped(text: str) -> str:
    return text if len(text) <= _MESSAGE_CHARS else text[:_MESSAGE_CHARS] + '…'


def _report_strategy(strategy: object, hook: str, what: str) -> None:
    logger.error(
        '期望: 长程策略 %s.%s 按协议交回一个动作。 实际: 它%s；这一处不纠偏，run 照常进行。'
        ' 下一步: 修好这个策略（四个方法都是 async、交回对应的动作类型），'
        '或换回 BaselineStrategy。',
        type(strategy).__name__,
        hook,
        what,
    )


class LongHorizonModule(RuntimeModule):
    """把长程任务接进 Runtime（spec §8.2–§8.6）。`acceptance` 是同一个 Runtime 里装的那个验收 Module
    （Goal、Rubric 与 L3 都来自它）；`strategy` 缺省是 `BaselineStrategy`；别的策略缺省按不可信对待
    它写下的文字，宿主读过它的代码再传 `trusted_strategy=True`。"""

    def __init__(
        self,
        acceptance: AcceptanceModule,
        *,
        strategy: LongHorizonStrategy | None = None,
        trusted_strategy: bool = False,
    ) -> None:
        from syndica.kernel.acceptance_module import AcceptanceModule

        if not isinstance(cast(object, acceptance), AcceptanceModule):
            raise ConfigError(
                expected='LongHorizonModule 的第一个参数是同一个 Runtime 里装的 AcceptanceModule',
                received=f'收到 {type(acceptance).__name__}',
                next_action=(
                    '先建 AcceptanceModule()，把它与 LongHorizonModule(acceptance) 一起交给 Runtime'
                ),
            )
        if strategy is not None:
            _validate_strategy(strategy)
        self.acceptance = acceptance
        self.strategy: LongHorizonStrategy = (
            strategy if strategy is not None else BaselineStrategy()
        )
        self.floor = (
            TrustLevel.TRUSTED if strategy is None or trusted_strategy else TrustLevel.UNTRUSTED
        )

        def plugin_factory(
            runtime: Runtime, agent: BaseAgent, flags: Mapping[str, FlagValue]
        ) -> _NoHooks:
            _validate_flags(flags)
            agent._attach_long_horizon(_Controller(self, runtime, agent, flags))  # pyright: ignore[reportPrivateUsage]
            return _NoHooks()

        super().__init__(
            'long-horizon',
            version=__version__,
            description='长程任务：进度账本、停滞 / 漂移 / 预算低的纠偏，策略可插拔（§8.2–§8.6）',
            capabilities=(),
            replay_safe=False,
            requires=('acceptance',),
            flags=(
                FlagDeclaration(
                    key='long_horizon_progress_every',
                    default=10,
                    description='每隔几轮跑一次里程碑 L3 量进度；0 = 只在完成时量',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='long_horizon_stall_turns',
                    default=20,
                    description='连续几轮没有新的验收条被满足就算停滞、请策略纠偏；0 = 不检测',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='long_horizon_budget_low_percent',
                    default=20,
                    description='预算剩不到百分之几时问策略怎么分配；0 = 不问',
                    minimum=0,
                    maximum=99,
                ),
                FlagDeclaration(
                    key='long_horizon_drift_window',
                    default=8,
                    description='每隔几轮检查一次最近的动作还锚不锚得回 Goal；0 = 不检查',
                    minimum=0,
                ),
                FlagDeclaration(
                    key='long_horizon_semantic_drift',
                    default=False,
                    description='锚点检查可疑时再做一次语义比对确认（一次伴生调用）',
                ),
                FlagDeclaration(
                    key='long_horizon_max_continuations',
                    default=3,
                    description='验收没过、打回也用完之后，策略最多让 Agent 接着干几次（每个周期）',
                    minimum=0,
                ),
            ),
            agent_plugins=(plugin_factory,),
        )


def _validate_strategy(strategy: object) -> None:
    missing = [
        hook for hook in _ACTIONS if not inspect.iscoroutinefunction(getattr(strategy, hook, None))
    ]
    if missing:
        raise ConfigError(
            expected='长程策略实现 LongHorizonStrategy 的四个 async 方法',
            received=f'{type(strategy).__name__} 缺少或不是 async 的：{missing}',
            next_action='照 LongHorizonStrategy 写 async def on_progress_stall / on_budget_low / '
            'on_goal_drift / should_stop；不确定就从 BaselineStrategy 继承',
        )


def _validate_flags(flags: Mapping[str, FlagValue]) -> None:
    if not flags['acceptance_outcome'] and (
        flags['long_horizon_progress_every'] or flags['long_horizon_stall_turns']
    ):
        raise ConfigError(
            expected='长程的进度靠 L3 验收：acceptance_outcome 开着',
            received='acceptance_outcome 是 false，而 long_horizon_progress_every / '
            'long_horizon_stall_turns 要量进度',
            next_action='打开 acceptance_outcome，或把这两个 Flag 设成 0（只要漂移与预算低的纠偏）',
        )
    if flags['long_horizon_stall_turns'] and not flags['long_horizon_progress_every']:
        raise ConfigError(
            expected='停滞检测要隔几轮量一次进度',
            received=(
                'long_horizon_stall_turns 大于 0，而 long_horizon_progress_every 是 0'
                '（只在完成时量）'
            ),
            next_action=(
                '给 long_horizon_progress_every 一个不大于 long_horizon_stall_turns 的正数，'
                '或把 long_horizon_stall_turns 设成 0'
            ),
        )
