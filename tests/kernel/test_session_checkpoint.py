"""本模块保留 `from __future__ import annotations`：仓库每个模块都带它，于是工具类的标注在运行时是
字符串、schema 反射按字符串取标注。少了它这里全绿、每个真实工具却会炸（见 test_runtime 的理由）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from syndica.kernel.agent import AgentOptions, BaseAgent
from syndica.kernel.context import ToolCall
from syndica.kernel.errors import ConfigError
from syndica.kernel.events import EventBus
from syndica.kernel.hooks import HooksCenter
from syndica.kernel.llm import ScriptedLLM, ScriptedTurn
from syndica.kernel.redaction import Finding, RuleRedactor
from syndica.kernel.runtime import AgentFactory, Runtime
from syndica.kernel.session_redaction import RedactedSessionRecord, redacted_record
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus
from syndica.kernel.stores import InMemorySessionStore
from syndica.kernel.tool import Tool, ToolResult

_TIMEOUT = 1.0
_SYSTEM_PROMPT = 'You are a worker.'
# 给了 sessions= 就必须给 redactor=（F4）：落盘前要脱敏，内核不读环境、默认脱敏器认不出宿主密钥值。
_REDACTOR = RuleRedactor()
# 三个工具，注册顺序既非升序（Alpha/Block/Charlie）亦非降序（Charlie/Block/Alpha），反转后同样两者
# 都不是：工具名按序逐字进请求前缀（spec §5.2），n<3 或顺序恰好有序时「保持顺序」与排序 / 反转同解。
_TOOL_ORDER = ['Block', 'Alpha', 'Charlie']


class _Block(Tool):
    """进入后挂起，直到测试放行。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self) -> ToolResult:
        """一直等到测试放行。"""
        self.entered.set()
        await self.release.wait()
        return ToolResult(output='released')


class _Alpha(Tool):
    async def execute(self) -> ToolResult:
        """什么都不做；只为占住工具顺序里的一格。"""
        return ToolResult(output='alpha')


class _Charlie(Tool):
    async def execute(self) -> ToolResult:
        """什么都不做；只为占住工具顺序里的一格。"""
        return ToolResult(output='charlie')


class _MarkerRedactor:
    """把字面量 SECRET 换成占位符的最小脱敏器：用它证明 Runtime 存盘走的是交进来的这一份。"""

    def redact(self, text: str) -> str:
        return text.replace('SECRET', '[REDACTED]')

    def scan(self, text: str) -> tuple[Finding, ...]:
        return ()


class _SpyStore(InMemorySessionStore):
    """记下每次 save 收到的对象：存盘面只收 RedactedSessionRecord，第三方 store 绕不过脱敏。"""

    def __init__(self) -> None:
        super().__init__()
        self.saved: list[object] = []

    async def save(self, sid: str, record: RedactedSessionRecord) -> None:
        self.saved.append(record)
        await super().save(sid, record)


class _FailingStore(InMemorySessionStore):
    async def save(self, sid: str, record: RedactedSessionRecord) -> None:
        raise RuntimeError('disk full')


def _blocking_factory(blocker: _Block, *, content: str = 'done') -> AgentFactory:
    llm = ScriptedLLM(
        [
            ScriptedTurn(tool_calls=[ToolCall(id='c1', name='Block', arguments='{}')]),
            ScriptedTurn(content=content),
        ]
    )

    def build(hooks: HooksCenter, events: EventBus) -> BaseAgent:
        return BaseAgent(
            AgentOptions(
                name='Worker',
                llm=llm,
                tools={'Block': blocker, 'Alpha': _Alpha(), 'Charlie': _Charlie()},
                system_prompt=_SYSTEM_PROMPT,
            ),
            hooks=hooks,
            events=events,
        )

    return build


async def _run_blocked_session(
    runtime: Runtime, *, mark_session: bool = True, content: str = 'done'
) -> str:
    """起一个会话根 Agent，停在工具调用里时标成会话根，放行后等它跑完一个周期（存盘在周期收尾）。

    在 Agent 被挡住时才写 session_id：spawn 立刻开跑，收尾前写入才不与周期赛跑。返回 agent_id。
    """
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking_factory(blocker, content=content), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    if mark_session:
        await runtime.registry.update(agent_id, session_id=agent_id)
    blocker.release.set()
    await runtime.wait(agent_id)
    return agent_id


async def test_checkpoint_saves_root_session_at_cycle_end() -> None:
    store = InMemorySessionStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    agent_id = await _run_blocked_session(runtime)

    record = await store.load(agent_id)
    assert record is not None
    assert record.meta.sid == agent_id
    assert record.meta.agent == 'Worker'
    assert record.meta.status is SessionStatus.ACTIVE
    assert record.meta.turns == 2
    assert record.system_prompt == _SYSTEM_PROMPT
    assert record.model == 'scripted'
    assert record.tools == _TOOL_ORDER
    assert record.flags == {}
    assert any(block.content == 'done' for block in record.content)
    await runtime.shutdown()


async def test_agent_without_session_id_is_not_checkpointed() -> None:
    store = InMemorySessionStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    agent_id = await _run_blocked_session(runtime, mark_session=False)

    assert await store.exists(agent_id) is False
    assert await store.list_metas() == []
    await runtime.shutdown()


async def test_checkpoint_runs_on_shutdown_while_running() -> None:
    store = InMemorySessionStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking_factory(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    await runtime.registry.update(agent_id, session_id=agent_id)
    # 不放行：Agent 还卡在工具里。shutdown 取消 + gather 之后存盘（SIGINT 情形），读的是终态（F9）。
    await runtime.shutdown()

    assert await store.exists(agent_id) is True
    record = await store.load(agent_id)
    assert record is not None
    assert record.meta.sid == agent_id


async def test_two_session_roots_do_not_cross() -> None:
    # 多 Agent 框架：sid = session_id（不是 agent_id），两个会话根互不串线。一个「只存第一个」或「把
    # 一个 Agent 的轨迹存到另一个 sid 下」的变异，单 Agent 用例无感（评审 F15）。
    store = InMemorySessionStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    blocker_a, blocker_b = _Block(), _Block()
    a = await runtime.spawn(_blocking_factory(blocker_a, content='alpha'), query='go')
    b = await runtime.spawn(_blocking_factory(blocker_b, content='bravo'), query='go')
    await asyncio.wait_for(blocker_a.entered.wait(), _TIMEOUT)
    await asyncio.wait_for(blocker_b.entered.wait(), _TIMEOUT)
    await runtime.registry.update(a, session_id='sess-A')
    await runtime.registry.update(b, session_id='sess-B')
    blocker_a.release.set()
    blocker_b.release.set()
    await runtime.wait(a)
    await runtime.wait(b)

    rec_a = await store.load('sess-A')
    rec_b = await store.load('sess-B')
    assert rec_a is not None and rec_b is not None
    assert any(block.content == 'alpha' for block in rec_a.content)
    assert any(block.content == 'bravo' for block in rec_b.content)
    # sid 是 session_id，不是 agent_id：按 agent_id 取不到。
    assert await store.exists(a) is False
    assert await store.exists(b) is False
    await runtime.shutdown()


async def test_runtime_without_session_store_is_a_noop() -> None:
    runtime = Runtime()
    agent_id = await _run_blocked_session(runtime)
    # 没配 SessionStore 时存盘整条跳过：收尾照常，Agent 跑完一个周期。
    assert await runtime.find(agent_id) is not None
    await runtime.shutdown()


async def test_checkpoint_routes_content_through_the_runtime_redactor() -> None:
    store = _SpyStore()
    runtime = Runtime(sessions=store, redactor=_MarkerRedactor())
    agent_id = await _run_blocked_session(runtime, content='token SECRET trailing')

    assert store.saved and isinstance(store.saved[-1], RedactedSessionRecord)
    record = await store.load(agent_id)
    assert record is not None
    blob = ' '.join(str(block.content) for block in record.content)
    assert 'SECRET' not in blob
    assert '[REDACTED]' in blob
    await runtime.shutdown()


async def test_checkpoint_preserves_created_at_title_and_archived() -> None:
    store = InMemorySessionStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    blocker = _Block()
    agent_id = await runtime.spawn(_blocking_factory(blocker), query='go')
    await asyncio.wait_for(blocker.entered.wait(), _TIMEOUT)
    await runtime.registry.update(agent_id, session_id=agent_id)
    # 预置一份已有元数据：收尾存盘必须接住这几个由更早存盘或一次 archive 定下的字段，而不是清掉。
    seeded = SessionRecord(
        meta=SessionMeta(
            sid=agent_id,
            title='kept title',
            agent='Worker',
            archived=7,
            created_at=123.0,
            updated_at=123.0,
        )
    )
    await store.save(agent_id, redacted_record(seeded, _MarkerRedactor()))
    blocker.release.set()
    await runtime.wait(agent_id)

    meta = await store.load_meta(agent_id)
    assert meta is not None
    assert meta.title == 'kept title'
    assert meta.archived == 7
    assert meta.created_at == 123.0
    assert meta.updated_at > 123.0
    await runtime.shutdown()


async def test_sessions_without_a_redactor_is_refused() -> None:
    # F4：给了 sessions= 却忘了 redactor=，会话会以「只认格式、认不出宿主密钥值」落盘——拒绝构造。
    with pytest.raises(ConfigError, match='redactor'):
        Runtime(sessions=InMemorySessionStore())


async def test_checkpoint_failure_is_logged_with_phase_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _FailingStore()
    runtime = Runtime(sessions=store, redactor=_REDACTOR)
    with caplog.at_level(logging.ERROR):
        agent_id = await _run_blocked_session(runtime)

    # 存盘抛异常不连累收尾：Agent 照常跑完一个周期（进了 ZOMBIE）。错误记 ERROR，带会话 id、失败
    # 阶段（save）与异常类型名——别把一切都误报成「磁盘满」（F8）。
    assert await runtime.find(agent_id) is not None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    matched = [r for r in errors if agent_id in r.getMessage()]
    assert matched
    message = matched[-1].getMessage()
    assert 'save' in message
    assert 'RuntimeError' in message
    await runtime.shutdown()
