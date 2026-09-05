"""AgentRegistry 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方
写 backend 时继承它并提供 registry fixture 即可验证合规。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.errors import SyndicaError
from syndica.kernel.protocols.registry import AgentRegistry

__all__ = ['AgentRegistryContract']


def _info(agent_id: str = 'a1', **kw: object) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        name='Worker',
        agent_type='declarative',
        **kw,  # pyright: ignore[reportArgumentType]
    )


@contextmanager
def _rejects(action: str) -> Generator[None]:
    """断言块内抛出 SyndicaError。不用 pytest.raises：kernel 只依赖标准库。"""
    try:
        yield
    except SyndicaError:
        return
    raise AssertionError(
        f'期望 {action} 抛出 SyndicaError，实际调用成功返回。'
        ' 让该实现在这条路径上抛 SyndicaError 子类，调用方才拿得到可自助修复的三件套。'
    )


class AgentRegistryContract:
    """子类必须提供一个 `registry` fixture，且每个用例拿到的必须是空注册表。"""

    async def test_register_then_get_roundtrips(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        got = await registry.get('a1')
        assert got is not None
        assert got.agent_id == 'a1'
        assert got.status is AgentStatus.IDLE

    async def test_get_unknown_returns_none(self, registry: AgentRegistry) -> None:
        assert await registry.get('missing') is None

    async def test_register_rejects_duplicate_agent_id(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        with _rejects('用已注册的 agent_id 再次 register'):
            await registry.register(_info())

    async def test_register_rejects_unknown_parent(self, registry: AgentRegistry) -> None:
        """父不在注册表时静默丢链接，会让 children_of 少报子代且无人察觉。"""
        with _rejects('register 一个 parent_id 未注册的 Agent'):
            await registry.register(_info('child', parent_id='root'))
        assert await registry.get('child') is None

    async def test_register_stores_snapshot_not_caller_reference(
        self, registry: AgentRegistry
    ) -> None:
        info = _info()
        await registry.register(info)
        info.status = AgentStatus.ABORTING
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.IDLE

    async def test_cas_succeeds_when_expectation_matches(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        ok = await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        )
        assert ok is True
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.RUNNING

    async def test_cas_fails_when_expectation_stale(self, registry: AgentRegistry) -> None:
        """状态转换必须是 CAS：多进程下并发转换不得静默丢失。"""
        await registry.register(_info())
        await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
        )
        ok = await registry.compare_and_set_status(
            'a1', expected=AgentStatus.IDLE, new=AgentStatus.PAUSED
        )
        assert ok is False
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.RUNNING

    async def test_cas_on_unknown_agent_returns_false(self, registry: AgentRegistry) -> None:
        assert (
            await registry.compare_and_set_status(
                'missing', expected=AgentStatus.IDLE, new=AgentStatus.RUNNING
            )
            is False
        )

    async def test_update_writes_single_writer_fields(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.update('a1', session_id='s1')
        got = await registry.get('a1')
        assert got is not None and got.session_id == 's1'

    async def test_update_on_unknown_agent_raises(self, registry: AgentRegistry) -> None:
        with _rejects('update 一个未注册的 agent_id'):
            await registry.update('missing', session_id='s1')

    async def test_update_rejects_unknown_field(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        with _rejects('update 一个 AgentInfo 上不存在的字段'):
            await registry.update('a1', bogus='x')

    async def test_update_rejects_children(self, registry: AgentRegistry) -> None:
        """children 由多方写入；无条件写会让父→子索引与 parent_id 脱钩。"""
        await registry.register(_info())
        with _rejects('update 写 children'):
            await registry.update('a1', children=['c1'])

    async def test_children_of_reflects_parent_links(self, registry: AgentRegistry) -> None:
        await registry.register(_info('root'))
        await registry.register(_info('child', parent_id='root'))
        assert await registry.children_of('root') == ['child']

    async def test_children_of_unknown_agent_returns_empty(self, registry: AgentRegistry) -> None:
        assert await registry.children_of('missing') == []

    async def test_children_of_returns_copy_not_live_list(self, registry: AgentRegistry) -> None:
        await registry.register(_info('root'))
        await registry.register(_info('child', parent_id='root'))
        kids = await registry.children_of('root')
        kids.append('ghost')
        assert await registry.children_of('root') == ['child']

    async def test_list_filters_by_status(self, registry: AgentRegistry) -> None:
        await registry.register(_info('a1'))
        await registry.register(_info('a2', status=AgentStatus.ZOMBIE))
        zombies = await registry.list(status=AgentStatus.ZOMBIE)
        assert [i.agent_id for i in zombies] == ['a2']

    async def test_list_filters_by_parent_id(self, registry: AgentRegistry) -> None:
        await registry.register(_info('root'))
        await registry.register(_info('child', parent_id='root'))
        await registry.register(_info('orphan'))
        assert [i.agent_id for i in await registry.list(parent_id='root')] == ['child']

    async def test_list_returns_snapshots_not_live_references(
        self, registry: AgentRegistry
    ) -> None:
        """内存实现放行调用方改存储、远端实现不放行，分叉只在换后端时才暴露。"""
        await registry.register(_info())
        listed = await registry.list()
        assert len(listed) == 1
        listed[0].status = AgentStatus.ABORTING
        again = await registry.list()
        assert again[0].status is AgentStatus.IDLE

    async def test_unregister_removes_entry(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.unregister('a1')
        assert await registry.get('a1') is None

    async def test_unregister_removes_child_from_parent(self, registry: AgentRegistry) -> None:
        await registry.register(_info('root'))
        await registry.register(_info('child', parent_id='root'))
        await registry.unregister('child')
        assert await registry.children_of('root') == []

    async def test_get_returns_snapshot_not_live_reference(self, registry: AgentRegistry) -> None:
        """调用方改动返回值不得影响存储——否则远端实现与内存实现行为不一致。"""
        await registry.register(_info())
        got = await registry.get('a1')
        assert got is not None
        got.status = AgentStatus.ABORTING
        again = await registry.get('a1')
        assert again is not None and again.status is AgentStatus.IDLE
