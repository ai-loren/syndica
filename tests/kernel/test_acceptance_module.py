"""验收 Runtime Module（§7.5–§7.7）：Goal 派生与注入、开工前投影、L1 零 token 打回、L2 / L3 接线、
只有根协调者验收、人的反馈提议、处理器抛出不当零违规。

本模块保留 `from __future__ import annotations` 与仓库其余模块一致。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from syndica.kernel.acceptance import RubricOrigin, RubricStatus, VerdictStatus
from syndica.kernel.acceptance_module import AcceptanceModule
from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.artifacts import ArtifactHandlers, ArtifactReader, Preview, Violation
from syndica.kernel.context import ArtifactRef, StopReason, ToolCall
from syndica.kernel.events import EventBus
from syndica.kernel.history_blocks import HistoryKind, kind_of
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.runtime import AgentFactory, ExitMode, Runtime
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore
from syndica.kernel.tool import Tool, ToolResult
from syndica.kernel.tools.local import Deliver, Write
from syndica.kernel.workspace_router import WorkspaceRouter

_TIMEOUT = 5.0
_GOAL = 'ship a report with a section four'
_FINAL = 'Final answer: the report now has a section four.'


def _call(name: str, call_id: str, **arguments: object) -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))]
    )


def _json(value: object) -> ScriptedTurn:
    return ScriptedTurn(content=json.dumps(value))


def _projection() -> ScriptedTurn:
    rubric = {
        'id': 'R1',
        'statement': 'the report has a section four',
        'kind': 'checkable',
        'anchors': [{'source': 'user_message', 'locator': 'msg:goal', 'quote': 'section four'}],
    }
    return _json({'rubrics': [rubric]})


def _satisfied() -> ScriptedTurn:
    evidence = [{'source': 'user_message', 'locator': 'reply', 'quote': 'the report now has'}]
    outcome = {'rubric_id': 'R1', 'result': 'satisfied', 'evidence': evidence}
    return _json({'outcomes': [outcome], 'reason': 'checked'})


def _setup(
    tmp_path: Path,
    module: AcceptanceModule,
    flags: dict[str, Any] | None = None,
) -> tuple[Runtime, WorkspaceRouter]:
    root = tmp_path / 'ws'
    root.mkdir()
    router = WorkspaceRouter(LocalWorkspace(str(root), shell='/bin/sh', env={}))
    runtime = Runtime(
        modules=[module], flags=flags or {}, workspace=router, artifacts=InMemoryArtifactStore()
    )
    return runtime, router


def _factory(
    llm: ScriptedLLM, router: WorkspaceRouter, *, own_deliver: bool = False
) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        portal = router.portal()
        tools: dict[str, Any] = {'Write': Write(portal)}
        if own_deliver:
            tools['Deliver'] = Deliver(portal)
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools=tools, system_prompt='SP', max_turns=12),
            hooks=hooks,
            events=events,
        )

    return build


async def _run(runtime: Runtime, router: WorkspaceRouter, llm: ScriptedLLM, query: str):  # type: ignore[no-untyped-def]
    agent_id = await runtime.spawn(_factory(llm, router), query=query, exit_mode=ExitMode.ZOMBIE)
    return agent_id, await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)


def _kinds(ctx: Any) -> list[HistoryKind | None]:
    return [kind_of(b) for b in ctx.current_traj]


async def test_an_empty_deliverable_is_sent_back_without_any_verifier_call(tmp_path: Path) -> None:
    # L1 零 token：默认 Flag（L2 / L3 都关、没有 Rubric）下照样打回；改好之后的回复没有裁决。
    runtime, router = _setup(tmp_path, AcceptanceModule())
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='report.md', content=''),
            _call('Deliver', 'c2', path='report.md'),
            ScriptedTurn(content='done'),
            _call('Write', 'c3', path='report.md', content='# Report\n'),
            _call('Deliver', 'c4', path='report.md'),
            ScriptedTurn(content='done again'),
        ]
    )
    try:
        _, ctx = await _run(runtime, router, llm, 'write the report')
        feedback = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.VERDICT]
        assert len(feedback) == 1 and 'report.md' in str(feedback[0].content)
        assert len(llm.calls) == 6  # 只有主 Agent 的六轮，没有伴生调用
        assert ctx.stop_reason is StopReason.COMPLETED
        assert ctx.verdict is None
    finally:
        await runtime.shutdown()


async def test_a_derived_goal_is_stored_but_not_injected_twice(tmp_path: Path) -> None:
    # Goal 就是第一条用户消息：存进 RubricStore，但不再注入一块同样的内容。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_outcome': True})
    try:
        agent_id, ctx = await _run(
            runtime, router, ScriptedLLM([ScriptedTurn(content=_FINAL)]), _GOAL
        )
        goal = await store.get_goal(agent_id)
        assert goal is not None and goal.intent == _GOAL
        assert HistoryKind.GOAL not in _kinds(ctx)
        assert ctx.verdict is not None and ctx.verdict.unverified_cause == 'no_active_rubrics'
    finally:
        await runtime.shutdown()


async def test_a_host_goal_is_projected_before_work_and_verified_at_the_end(tmp_path: Path) -> None:
    store = InMemoryRubricStore()
    module = AcceptanceModule(store=store, goal=_GOAL)
    flags = {'acceptance_rubrics': True, 'acceptance_outcome': True}
    runtime, router = _setup(tmp_path, module, flags)
    llm = ScriptedLLM([_projection(), ScriptedTurn(content=_FINAL), _satisfied()])
    try:
        _, ctx = await _run(runtime, router, llm, 'please do it')
        main_request = llm.calls[1]  # 第一次是投影；主 Agent 第一轮就看得到 Goal 与 Rubric
        assert any(kind_of(b) is HistoryKind.GOAL for b in main_request)
        assert any(kind_of(b) is HistoryKind.RUBRIC for b in main_request)
        assert ctx.verdict is not None and ctx.verdict.status is VerdictStatus.PASS
    finally:
        await runtime.shutdown()


async def test_a_child_agent_has_no_goal_and_no_verdict(tmp_path: Path) -> None:
    # 只有根协调者有 Goal：子 Agent 交回的东西由派它的那一方验收，不各付一份伴生的钱。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_outcome': True})
    try:
        root_id, _ = await _run(runtime, router, ScriptedLLM([ScriptedTurn(content=_FINAL)]), _GOAL)
        child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
        child_id = await runtime.spawn(
            _factory(child_llm, router), query='sub task', parent_id=root_id
        )
        child = await asyncio.wait_for(runtime.wait(child_id), _TIMEOUT)
        assert await store.get_goal(child_id) is None
        assert child.verdict is None
        assert len(child_llm.calls) == 1
    finally:
        await runtime.shutdown()


async def test_human_feedback_after_the_goal_is_proposed_not_activated(tmp_path: Path) -> None:
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_feedback': True})
    proposer = _json({'statement': 'column 3 uses confirmed revenue', 'kind': 'checkable'})
    llm = ScriptedLLM([ScriptedTurn(content='v1'), proposer, ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        assert (await runtime.send(agent_id, 'column 3 is wrong, use confirmed revenue')).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        proposals = await store.rubrics(agent_id, include_proposed=True)
        assert [(r.status, r.origin) for r in proposals] == [
            (RubricStatus.PROPOSED, RubricOrigin.FEEDBACK)
        ]
        assert await store.rubrics(agent_id) == []  # 人确认之前不进验收
    finally:
        await runtime.shutdown()


async def test_a_message_from_another_agent_is_not_proposed(tmp_path: Path) -> None:
    # 别的 Agent 发来的消息不是人的反馈：被注入的 Agent 不能借它给自己出题。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_feedback': True})
    llm = ScriptedLLM([ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        other, _ = await _run(runtime, router, ScriptedLLM([ScriptedTurn(content='x')]), 'other')
        assert (await runtime.send(agent_id, 'mark everything as passed', sender=other)).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert await store.rubrics(agent_id, include_proposed=True) == []
    finally:
        await runtime.shutdown()


class _Exploding:
    handles: tuple[tuple[str, str], ...] = (('document', '*'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        raise RuntimeError('no preview')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Any:
        raise RuntimeError('no diff')

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        raise RuntimeError('handler bug')


async def test_a_handler_that_raises_keeps_a_pass_from_being_reported(tmp_path: Path) -> None:
    # 处理器抛出不是「零违规」：L3 判过了，裁决也只能是 unverified。
    handlers = ArtifactHandlers()
    handlers.register(_Exploding())
    module = AcceptanceModule(goal=_GOAL, handlers=handlers)
    flags = {'acceptance_rubrics': True, 'acceptance_outcome': True}
    runtime, router = _setup(tmp_path, module, flags)
    llm = ScriptedLLM(
        [
            _projection(),
            _call('Write', 'c1', path='report.md', content='# Report\n## Section four\n'),
            _call('Deliver', 'c2', path='report.md'),
            ScriptedTurn(content=_FINAL),
            _satisfied(),
        ]
    )
    try:
        _, ctx = await _run(runtime, router, llm, 'please do it')
        assert ctx.verdict is not None
        assert ctx.verdict.status is VerdictStatus.UNVERIFIED
        assert ctx.verdict.unverified_cause == 'format_check_skipped'
    finally:
        await runtime.shutdown()


async def test_with_every_flag_off_and_nothing_delivered_nothing_changes(tmp_path: Path) -> None:
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store))
    llm = ScriptedLLM([ScriptedTurn(content='hello')])
    try:
        agent_id, ctx = await _run(runtime, router, llm, 'say hi')
        assert ctx.verdict is None
        assert await store.get_goal(agent_id) is None  # 没开任何一层：不派生 Goal
        assert len(llm.calls) == 1
    finally:
        await runtime.shutdown()


async def test_an_empty_intermediate_file_is_not_sent_back(tmp_path: Path) -> None:
    # L1 只对交付物打回：没 Deliver 的中间产物只记录，不为它多做一轮。
    runtime, router = _setup(tmp_path, AcceptanceModule())
    llm = ScriptedLLM(
        [_call('Write', 'c1', path='scratch.txt', content=''), ScriptedTurn(content='done')]
    )
    try:
        _, ctx = await _run(runtime, router, llm, 'think out loud')
        assert HistoryKind.VERDICT not in _kinds(ctx)
        assert len(llm.calls) == 2
    finally:
        await runtime.shutdown()


async def test_the_goal_is_projected_once_not_every_cycle(tmp_path: Path) -> None:
    module = AcceptanceModule(goal=_GOAL)
    runtime, router = _setup(tmp_path, module, {'acceptance_rubrics': True})
    llm = ScriptedLLM([_projection(), ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'please do it')
        assert (await runtime.send(agent_id, 'and once more')).ok
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert len(llm.calls) == 3
        assert ctx.current_traj[-1].content == 'v2'
    finally:
        await runtime.shutdown()


async def test_the_message_the_goal_came_from_is_not_proposed_as_feedback(tmp_path: Path) -> None:
    from syndica.kernel.acceptance import Anchor, AnchorSource, Goal
    from syndica.kernel.acceptance_module import (
        _AgentAcceptance,  # pyright: ignore[reportPrivateUsage]
    )
    from syndica.kernel.message import Message
    from syndica.kernel.redaction import RuleRedactor
    from syndica.kernel.rubric_redaction import redacted_goal

    store = InMemoryRubricStore()
    module = AcceptanceModule(store=store)
    runtime, router = _setup(tmp_path, module, {'acceptance_feedback': True})
    try:
        agent_id, _ = await _run(runtime, router, ScriptedLLM([ScriptedTurn(content='ok')]), 'x')
        task = Message(sender='runtime', receiver=agent_id, content='build the sales table')
        anchor = Anchor(AnchorSource.USER_MESSAGE, f'msg:{task.id}', task.content)
        goal = Goal(intent=task.content, anchors=(anchor,))
        await store.put_goal(agent_id, redacted_goal(goal, RuleRedactor()))
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None
        flags = {decl.key: decl.default for decl in module.flags}
        wiring = _AgentAcceptance(module, runtime, agent, {**flags, 'acceptance_feedback': True})
        await wiring.on_message(ctx=agent.context, message=task)
        assert await store.rubrics(agent_id, include_proposed=True) == []
    finally:
        await runtime.shutdown()


async def test_a_failed_projection_is_not_retried_every_cycle(tmp_path: Path) -> None:
    # 投影失败按空集合处理；同一个 Goal 本进程只试一次，不让每个周期都多付一次伴生。
    module = AcceptanceModule(goal=_GOAL)
    runtime, router = _setup(tmp_path, module, {'acceptance_rubrics': True})
    llm = ScriptedLLM(
        [ScriptedTurn(content='not json'), ScriptedTurn(content='v1'), ScriptedTurn(content='v2')]
    )
    try:
        agent_id, _ = await _run(runtime, router, llm, 'please do it')
        assert (await runtime.send(agent_id, 'and once more')).ok
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert len(llm.calls) == 3
        assert ctx.current_traj[-1].content == 'v2'
    finally:
        await runtime.shutdown()


async def test_the_module_brings_deliver_unless_the_role_already_has_it(tmp_path: Path) -> None:
    # 没装验收的 run 不为 Deliver 多付一个工具定义；角色自己带了 Deliver 也不撞名。
    runtime, router = _setup(tmp_path, AcceptanceModule())
    try:
        given = await runtime.spawn(_factory(ScriptedLLM([]), router), exit_mode=ExitMode.ZOMBIE)
        own = await runtime.spawn(
            _factory(ScriptedLLM([]), router, own_deliver=True), exit_mode=ExitMode.ZOMBIE
        )
        for agent_id in (given, own):
            agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
            assert agent is not None and 'Deliver' in agent.options.tools
    finally:
        await runtime.shutdown()


class _QuietWrite(Tool):
    """写文件但不回报产物：Bash 改文件时内核看到的就是这样。"""

    __tool_name__ = 'QuietWrite'

    def __init__(self, router: WorkspaceRouter) -> None:
        self._router = router

    async def execute(self, path: str, content: str) -> ToolResult:
        """写入 content。"""
        await self._router.portal().write_text(path, content)
        return ToolResult(output='ok')


def _quiet_factory(llm: ScriptedLLM, router: WorkspaceRouter) -> AgentFactory:
    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        portal = router.portal()
        tools: dict[str, Any] = {'Write': Write(portal), 'QuietWrite': _QuietWrite(router)}
        return BaseAgent(
            AgentOptions(name='Worker', llm=llm, tools=tools, system_prompt='SP', max_turns=12),
            hooks=hooks,
            events=events,
        )

    return build


async def test_a_deliverable_broken_after_deliver_is_still_caught(tmp_path: Path) -> None:
    # 交付之后被命令改坏（没有登记），完成时按它此刻的版本复核（收口评审 H2）。
    runtime, router = _setup(tmp_path, AcceptanceModule())
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='report.md', content='# Report\n'),
            _call('Deliver', 'c2', path='report.md'),
            _call('QuietWrite', 'c3', path='report.md', content=''),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='done again'),
        ]
    )
    try:
        agent_id = await runtime.spawn(
            _quiet_factory(llm, router), query='go', exit_mode=ExitMode.ZOMBIE
        )
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert _kinds(ctx).count(HistoryKind.VERDICT) == 1
    finally:
        await runtime.shutdown()


async def test_an_earlier_cycles_problem_does_not_send_back_an_unrelated_reply(
    tmp_path: Path,
) -> None:
    runtime, router = _setup(tmp_path, AcceptanceModule(), {'acceptance_output_retries': 0})
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='report.md', content=''),
            _call('Deliver', 'c2', path='report.md'),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='4'),
        ]
    )
    try:
        agent_id, first = await _run(runtime, router, llm, 'write the report')
        assert first.verdict is not None and first.verdict.status is VerdictStatus.PARTIAL
        assert (await runtime.send(agent_id, 'unrelated: what is 2+2?')).ok
        second = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert second.verdict is None
        assert second.current_traj[-1].content == '4'
    finally:
        await runtime.shutdown()


class _StrictSheets:
    handles: tuple[tuple[str, str], ...] = (('spreadsheet', '*'),)

    async def preview(self, ref: ArtifactRef, reader: ArtifactReader) -> Preview:
        return Preview(text='')

    async def semantic_diff(self, a: ArtifactRef, b: ArtifactRef, reader: ArtifactReader) -> Any:
        raise RuntimeError('no diff')

    async def validate(self, ref: ArtifactRef, reader: ArtifactReader) -> list[Violation]:
        return [Violation(rule='columns', message='第 3 行少一列')]


async def test_delivering_under_another_kind_does_not_escape_the_strict_handler(
    tmp_path: Path,
) -> None:
    # Deliver 的 kind 只是标签：L1 按扩展名派生的 kind 选处理器（收口评审 H3）。
    handlers = ArtifactHandlers()
    handlers.register(_StrictSheets())
    runtime, router = _setup(tmp_path, AcceptanceModule(handlers=handlers))
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='sales.csv', content='a,b,c\n1,2\n'),
            _call('Deliver', 'c2', path='sales.csv', kind='file'),
            ScriptedTurn(content='done'),
            ScriptedTurn(content='done again'),
        ]
    )
    try:
        _, ctx = await _run(runtime, router, llm, 'export the sales')
        feedback = [b for b in ctx.current_traj if kind_of(b) is HistoryKind.VERDICT]
        assert len(feedback) == 1 and '少一列' in str(feedback[0].content)
    finally:
        await runtime.shutdown()


async def test_a_crashed_handler_with_only_l1_installed_is_unverified(tmp_path: Path) -> None:
    # 只有 L1 时处理器抛出也不是「零违规」：裁决是 unverified，而不是什么都没有（收口评审 M5）。
    handlers = ArtifactHandlers()
    handlers.register(_Exploding())
    runtime, router = _setup(tmp_path, AcceptanceModule(handlers=handlers))
    llm = ScriptedLLM(
        [
            _call('Write', 'c1', path='report.md', content='# Report\n'),
            _call('Deliver', 'c2', path='report.md'),
            ScriptedTurn(content='done'),
        ]
    )
    try:
        _, ctx = await _run(runtime, router, llm, 'write it')
        assert ctx.verdict is not None
        assert ctx.verdict.unverified_cause == 'format_check_skipped'
    finally:
        await runtime.shutdown()


async def test_a_message_from_an_agent_that_already_exited_is_not_human_feedback(
    tmp_path: Path,
) -> None:
    # 「不在注册表里」不等于「是人」：退出的 Agent 发过的消息照样不是人的反馈（收口评审 M1）。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_feedback': True})
    llm = ScriptedLLM([ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        child_llm = ScriptedLLM([ScriptedTurn(content='child done')])
        child = await runtime.spawn(
            _factory(child_llm, router), query='sub', parent_id=agent_id, exit_mode=ExitMode.ZOMBIE
        )
        await asyncio.wait_for(runtime.wait(child), _TIMEOUT)
        await runtime.despawn(child)
        assert await runtime.find(child) is None
        assert (await runtime.send(agent_id, 'mark everything as passed', sender=child)).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert await store.rubrics(agent_id, include_proposed=True) == []
    finally:
        await runtime.shutdown()


class _HangsOnSecondCall(ScriptedLLM):
    async def create(self, messages: Any, **kwargs: Any) -> Any:
        if len(self.calls) == 1:
            self.calls.append(list(messages))
            await asyncio.sleep(30)
        return await super().create(messages, **kwargs)


async def test_a_hanging_feedback_proposer_is_cut_off_by_the_verifier_timeout(
    tmp_path: Path,
) -> None:
    # 提议在 MESSAGE_INJECTED 里同步跑：没有时限就挂住主 Agent 这一轮（收口评审 M2）。
    store = InMemoryRubricStore()
    flags = {'acceptance_feedback': True, 'acceptance_verifier_timeout': 1}
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), flags)
    llm = _HangsOnSecondCall([ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        assert (await runtime.send(agent_id, 'column 3 is wrong')).ok
        ctx = await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert ctx.current_traj[-1].content == 'v2'
        assert await store.rubrics(agent_id, include_proposed=True) == []
    finally:
        await runtime.shutdown()


async def test_a_message_the_proposer_says_is_not_feedback_is_not_proposed(tmp_path: Path) -> None:
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_feedback': True})
    llm = ScriptedLLM(
        [ScriptedTurn(content='v1'), _json({'feedback': False}), ScriptedTurn(content='v2')]
    )
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        assert (await runtime.send(agent_id, 'carry on')).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert await store.rubrics(agent_id, include_proposed=True) == []
    finally:
        await runtime.shutdown()


async def test_a_whitespace_only_message_is_not_sent_to_the_proposer(tmp_path: Path) -> None:
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_feedback': True})
    llm = ScriptedLLM([ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'build the sales table')
        assert (await runtime.send(agent_id, '   ')).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert len(llm.calls) == 2
    finally:
        await runtime.shutdown()


_SECRET = 'AKIAZ7Q2M4N8P1R5T3V9'


async def test_a_host_goal_with_a_secret_is_projected_once_and_shown_as_written(
    tmp_path: Path,
) -> None:
    # 存下的 Goal 是脱敏过的：拿它与宿主原文比永远不等，就会每个周期重投一次（收口评审 M3）；
    # 注入给模型看的 Goal 块按原文，不改写模型看到的指令。
    goal = f'{_GOAL} using key {_SECRET}'
    module = AcceptanceModule(goal=goal)
    runtime, router = _setup(tmp_path, module, {'acceptance_rubrics': True})
    llm = ScriptedLLM([_projection(), ScriptedTurn(content='v1'), ScriptedTurn(content='v2')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'please do it')
        assert (await runtime.send(agent_id, 'and once more')).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        assert len(llm.calls) == 3
        [shown] = [b for b in llm.calls[1] if kind_of(b) is HistoryKind.GOAL]
        assert _SECRET in str(shown.content)
    finally:
        await runtime.shutdown()


async def test_a_goal_change_that_projects_nothing_supersedes_the_old_rubrics(
    tmp_path: Path,
) -> None:
    # 投影失败把旧 Rubric 全退役了：轨迹里也要有一块替代，不让旧标准仍是最新、受保护的那块（M4）。
    module = AcceptanceModule(goal=_GOAL)
    runtime, router = _setup(tmp_path, module, {'acceptance_rubrics': True})
    llm = ScriptedLLM(
        [
            _projection(),
            ScriptedTurn(content='v1'),
            ScriptedTurn(content='not json'),
            ScriptedTurn(content='v2'),
        ]
    )
    try:
        agent_id, _ = await _run(runtime, router, llm, 'please do it')
        module.goal = 'translate the README into French'
        assert (await runtime.send(agent_id, 'new plan')).ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        latest = [b for b in llm.calls[3] if kind_of(b) is HistoryKind.RUBRIC][-1]
        assert '当前没有生效的标准' in str(latest.content)
    finally:
        await runtime.shutdown()


async def test_an_agent_that_cannot_write_is_not_offered_deliver(tmp_path: Path) -> None:
    runtime, _ = _setup(tmp_path, AcceptanceModule())

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        llm = ScriptedLLM([])
        return BaseAgent(AgentOptions(name='Reader', llm=llm, tools={}), hooks=hooks, events=events)

    try:
        agent_id = await runtime.spawn(build, exit_mode=ExitMode.ZOMBIE)
        agent = runtime._table.get(agent_id)  # pyright: ignore[reportPrivateUsage]
        assert agent is not None and 'Deliver' not in agent.options.tools
    finally:
        await runtime.shutdown()


async def test_a_host_goal_is_injected_even_with_every_layer_off(tmp_path: Path) -> None:
    # `--goal` 不被静默忽略：没开任何一层也存下并注入（收口评审 L5）。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store, goal=_GOAL))
    llm = ScriptedLLM([ScriptedTurn(content='done')])
    try:
        agent_id, _ = await _run(runtime, router, llm, 'please do it')
        assert (await store.get_goal(agent_id)) is not None
        assert any(kind_of(b) is HistoryKind.GOAL for b in llm.calls[0])
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize('goal', [123, '   '])
def test_a_goal_that_is_not_a_sentence_is_refused(goal: object) -> None:
    from syndica.kernel.errors import ConfigError

    with pytest.raises(ConfigError):
        AcceptanceModule(goal=goal)  # type: ignore[arg-type]


async def test_the_derived_goal_anchors_to_the_message_it_came_from(tmp_path: Path) -> None:
    # chat 的任务经邮箱到：锚点要能指回那一条消息，而不是一个所有任务共用的 `msg:task`。
    store = InMemoryRubricStore()
    runtime, router = _setup(tmp_path, AcceptanceModule(store=store), {'acceptance_outcome': True})
    llm = ScriptedLLM([ScriptedTurn(content=_FINAL)])
    try:
        agent_id = await runtime.spawn(_factory(llm, router), exit_mode=ExitMode.ZOMBIE)
        delivered = await runtime.send(agent_id, _GOAL)
        assert delivered.ok
        await asyncio.wait_for(runtime.wait(agent_id), _TIMEOUT)
        goal = await store.get_goal(agent_id)
        assert goal is not None
        assert [a.locator for a in goal.anchors] == [f'msg:{delivered.message_id}']
    finally:
        await runtime.shutdown()


async def test_the_reader_refuses_a_version_that_is_no_longer_on_disk(tmp_path: Path) -> None:
    from syndica.kernel.acceptance_module import (
        _PortalReader,  # pyright: ignore[reportPrivateUsage]
    )
    from syndica.kernel.errors import ConfigError

    root = tmp_path / 'ws'
    root.mkdir()
    portal = LocalWorkspace(str(root), shell='/bin/sh', env={}).portal()
    version = await portal.write_text('report.md', 'one\n')
    ref = ArtifactRef(
        id='file:report.md',
        path='report.md',
        kind='document',
        mime='text/markdown',
        version=version,
        producer='w',
    )
    assert await _PortalReader(portal).read(ref) == 'one\n'
    await portal.write_text('report.md', 'two\n')
    with pytest.raises(ConfigError):
        await _PortalReader(portal).read(ref)
