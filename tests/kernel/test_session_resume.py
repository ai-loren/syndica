"""本模块保留 `from __future__ import annotations`：仓库每个模块都带它（理由见 test_runtime）。"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ContextBlock, TrustLevel, provenance_of
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.session_redaction import redacted_record
from syndica.kernel.session_types import SessionMeta, SessionRecord
from syndica.kernel.stores import InMemorySessionStore

_TIMEOUT = 1.0
_REDACTOR = RuleRedactor()


def _worker_factory(*contents: str) -> AgentFactory:
    script = [ScriptedTurn(content=content) for content in contents] or [ScriptedTurn(content='ok')]

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(name='Worker', llm=ScriptedLLM(script), system_prompt='SP'),
            hooks=hooks,
            events=events,
        )

    return build


def _seed_record(
    sid: str,
    *,
    agent: str = 'Worker',
    turns: int = 3,
    content: list[ContextBlock] | None = None,
) -> SessionRecord:
    blocks = [ContextBlock(role='user', content='earlier turn')] if content is None else content
    return SessionRecord(
        meta=SessionMeta(sid=sid, agent=agent, turns=turns),
        content=blocks,
        system_prompt='SP',
    )


async def _store_with(record: SessionRecord) -> InMemorySessionStore:
    store = InMemorySessionStore()
    await store.save(record.meta.sid, redacted_record(record, _REDACTOR))
    return store


async def test_ensure_active_rebuilds_a_root_session_from_the_store() -> None:
    store = await _store_with(_seed_record('sess-1', turns=3))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)

    assert await runtime.ensure_active('sess-1') is True

    info = await runtime.find('sess-1')
    assert info is not None
    assert info.session_id == 'sess-1'
    agent = runtime._table.get('sess-1')  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    assert agent._current_turn == 3  # pyright: ignore[reportPrivateUsage]
    blocks = agent.context.current_traj
    assert any(block.content == 'earlier turn' for block in blocks)
    # 恢复提示是内核署名的历史块，带在轨迹里。
    assert any('恢复' in str(block.content) for block in blocks)
    await runtime.shutdown()


async def test_resume_is_a_noop_when_the_session_is_absent() -> None:
    store = InMemorySessionStore()
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    assert await runtime.ensure_active('absent') is False
    await runtime.shutdown()


async def test_resume_without_a_session_store_is_a_noop() -> None:
    runtime = Runtime(agents={'Worker': _worker_factory()})
    assert await runtime.ensure_active('anything') is False
    await runtime.shutdown()


async def test_resume_with_an_unregistered_role_logs_and_refuses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = await _store_with(_seed_record('sess-x', agent='Ghost'))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    with caplog.at_level(logging.ERROR):
        assert await runtime.ensure_active('sess-x') is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any('Ghost' in r.getMessage() and 'sess-x' in r.getMessage() for r in errors)
    await runtime.shutdown()


async def test_on_missing_queue_path_does_not_resume_from_store() -> None:
    # Resume contract：恢复是显式动作。send 的懒激活（_on_missing_queue）对只在磁盘上的会话一次
    # store 都不碰、不重建——否则一条散落消息就会把整份会话复活起来。
    store = await _store_with(_seed_record('sess-2'))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    assert await runtime._ensure_active('sess-2', resume_from_store=False) is False  # pyright: ignore[reportPrivateUsage]
    assert runtime._table.get('sess-2') is None  # pyright: ignore[reportPrivateUsage]
    # 显式 ensure_active 才恢复。
    assert await runtime.ensure_active('sess-2') is True
    await runtime.shutdown()


async def test_resume_carries_untrusted_provenance_past_the_trailing_trim() -> None:
    # 存档末尾是未配对的 assistant 批次，且那份轨迹里唯一的不可信标记就在它上面；_forkable_prefix
    # 会把它截掉。恢复必须把溯源结论带到 resume_note 上，否则 provenance 被洗成 TRUSTED（F3 同型）。
    from syndica.kernel.context import ToolCall

    content = [
        ContextBlock(role='user', content='ok', trust=TrustLevel.TRUSTED),
        ContextBlock(
            role='assistant',
            content='web said something',
            tool_calls=[ToolCall(id='c1', name='Fetch', arguments='{}')],
            trust=TrustLevel.UNTRUSTED,
        ),
    ]
    store = await _store_with(_seed_record('sess-3', content=content))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    assert await runtime.ensure_active('sess-3') is True
    agent = runtime._table.get('sess-3')  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    assert provenance_of(agent.context) is TrustLevel.UNTRUSTED
    await runtime.shutdown()


async def test_resumed_session_continues_and_recheckpoints() -> None:
    store = await _store_with(_seed_record('sess-4', turns=2))
    runtime = Runtime(
        agents={'Worker': _worker_factory('continued answer')},
        sessions=store,
        redactor=_REDACTOR,
    )
    assert await runtime.ensure_active('sess-4') is True
    # 续跑一轮：给它发一条消息，它处理后回 IDLE；turn 从种回的计数往上走。
    await runtime.send('sess-4', 'keep going')
    await asyncio.sleep(0)
    await asyncio.wait_for(_until_turn(runtime, 'sess-4', 3), _TIMEOUT)
    await runtime.shutdown()

    record = await store.load('sess-4')
    assert record is not None
    assert record.meta.turns >= 3
    assert any(block.content == 'earlier turn' for block in record.content)
    assert any(block.content == 'continued answer' for block in record.content)


async def _until_turn(runtime: Runtime, sid: str, target: int) -> None:
    while True:
        agent = runtime._table.get(sid)  # pyright: ignore[reportPrivateUsage]
        if agent is not None and agent._current_turn >= target:  # pyright: ignore[reportPrivateUsage]
            return
        await asyncio.sleep(0.01)


def _flagged(flags: dict[str, bool | int | str]) -> SessionRecord:
    record = _seed_record('sess-flags')
    record.flags = dict(flags)
    return record


async def test_a_stricter_recorded_safety_flag_survives_a_looser_config(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 换一份更松的配置续跑，不能放松一份当初在更严设置下开始的会话（M6 P28-M7）。
    from syndica.kernel.modules import SafetyModule

    store = await _store_with(_flagged({'untrusted_high_risk': 'reject', 'max_wait_seconds': 30}))
    runtime = Runtime(
        agents={'Worker': _worker_factory()},
        sessions=store,
        redactor=_REDACTOR,
        modules=[SafetyModule()],
        flags={'untrusted_high_risk': 'ask', 'max_wait_seconds': 0},
    )
    with caplog.at_level(logging.WARNING):
        assert await runtime.ensure_active('sess-flags') is True
    info = await runtime.find('sess-flags')
    assert info is not None
    assert (info.flags['untrusted_high_risk'], info.flags['max_wait_seconds']) == ('reject', 30)
    kept = [r.args for r in caplog.records if '更严' in str(r.msg)]
    assert sorted(str(cast(tuple[object, ...], args)[1]) for args in kept) == [
        'max_wait_seconds',
        'untrusted_high_risk',
    ]
    await runtime.shutdown()


async def test_a_looser_recorded_safety_flag_does_not_loosen_the_current_config() -> None:
    from syndica.kernel.modules import SafetyModule

    store = await _store_with(_flagged({'untrusted_high_risk': 'ask', 'max_wait_seconds': 60}))
    runtime = Runtime(
        agents={'Worker': _worker_factory()},
        sessions=store,
        redactor=_REDACTOR,
        modules=[SafetyModule()],
        flags={'untrusted_high_risk': 'reject', 'max_wait_seconds': 20},
    )
    assert await runtime.ensure_active('sess-flags') is True
    info = await runtime.find('sess-flags')
    assert info is not None
    assert (info.flags['untrusted_high_risk'], info.flags['max_wait_seconds']) == ('reject', 20)
    await runtime.shutdown()


async def test_recorded_safety_flags_do_not_block_a_runtime_without_the_safety_module() -> None:
    store = await _store_with(_flagged({'untrusted_high_risk': 'reject', 'max_wait_seconds': 30}))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    assert await runtime.ensure_active('sess-flags') is True
    await runtime.shutdown()


async def test_the_resume_note_names_the_agents_that_did_not_survive() -> None:
    # 只说「子 Agent 失效了」认不出是哪些 id：点名它们，模型才知道哪些 id 不能再发。
    content = [
        ContextBlock(role='user', content='earlier turn'),
        ContextBlock(
            role='tool',
            tool_call_id='d1',
            content='done',
            extra={'tool_meta': {'agent_id': 'child-a'}},
        ),
        ContextBlock(
            role='tool',
            tool_call_id='d2',
            content='sent',
            extra={'tool_meta': {'agent_id': 'child-b'}},
        ),
        ContextBlock(
            role='tool',
            tool_call_id='self',
            content='to myself',
            extra={'tool_meta': {'agent_id': 'sess-kids'}},
        ),
        ContextBlock(
            role='tool',
            tool_call_id='d3',
            content='again',
            extra={'tool_meta': {'agent_id': 'child-a'}},
        ),
    ]
    store = await _store_with(_seed_record('sess-kids', content=content))
    runtime = Runtime(agents={'Worker': _worker_factory()}, sessions=store, redactor=_REDACTOR)
    assert await runtime.ensure_active('sess-kids') is True
    agent = runtime._table.get('sess-kids')  # pyright: ignore[reportPrivateUsage]
    assert agent is not None
    [note] = [b for b in agent.context.current_traj if '恢复' in str(b.content)]
    assert '（child-a、child-b）' in str(note.content)
    await runtime.shutdown()
