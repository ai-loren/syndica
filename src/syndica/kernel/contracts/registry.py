"""AgentRegistry 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方
写 backend 时继承它并提供 registry fixture 即可验证合规。

**运行前提**见 `syndica.kernel.contracts.base`：async 用例要有插件驱动，
继承自 `ContractSuite` 的守卫会把配置缺失变成显式失败。

**CAS 必须跨过「读然后写」那次必然的让出仍然恰好一个赢家**（spec §4.4 / §13.3）。远端 store
在 GET 与 SET 之间必有一次往返；内存实现把两者放在同一次不让出的临界区里就能过，一个在中间
`await asyncio.sleep(0)` 的实现会让并发转换丢失。可接受的实现路径：Lua EVAL、WATCH / MULTI /
EXEC、或一次事务。基数取 3：n=2 下有退化解。
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import fields

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.errors import SyndicaError
from syndica.kernel.protocols.registry import (
    UPDATE_PROTECTED_FIELD_OWNERS,
    UPDATE_PROTECTED_FIELDS,
    UPDATE_WRITABLE_FIELDS,
    AgentRegistry,
)

__all__ = ['AgentRegistryContract']


def _info(agent_id: str = 'a1', **kw: object) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        name='Worker',
        agent_type='declarative',
        **kw,  # pyright: ignore[reportArgumentType]
    )


class _Uncopyable:
    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise RuntimeError('cannot copy')


@contextmanager
def _rejects(action: str) -> Generator[None]:
    """断言块内抛出 SyndicaError。kernel 只依赖标准库，本套件不得 import pytest。"""
    try:
        yield
    except SyndicaError:
        return
    raise AssertionError(
        f'期望 {action} 抛出 SyndicaError，实际调用成功返回。'
        ' 让该实现在这条路径上抛 SyndicaError 子类，调用方才拿得到可自助修复的三件套。'
    )


class AgentRegistryContract(ContractSuite):
    """子类必须提供一个 `registry` fixture，且每个用例拿到的必须是空注册表。

    套件另外要求实现满足两条不变式，二者都被用例依赖但不体现在方法签名上：

    - **读己所写**：写操作返回后，紧接着的读必须看到它。读落后副本的实现会挂在
      roundtrip 类用例上。
    - **拒绝即无副作用**：被拒绝的写不得留下部分写入。非事务性后端因此必须先校验后写入，
      或者自己回滚。
    """

    def test_update_field_sets_partition_agent_info_fields(self) -> None:
        overlap = UPDATE_WRITABLE_FIELDS & UPDATE_PROTECTED_FIELDS
        assert not overlap, (
            '期望两个字段集合互不相交。'
            f' 实际两边都有: {sorted(overlap)}。'
            ' 一个字段要么单一写者可写，要么另有专用机制拥有，只能二选一。'
        )
        declared = {f.name for f in fields(AgentInfo)}
        unclassified = declared - UPDATE_WRITABLE_FIELDS - UPDATE_PROTECTED_FIELDS
        stale = (UPDATE_WRITABLE_FIELDS | UPDATE_PROTECTED_FIELDS) - declared
        assert not unclassified and not stale, (
            '期望两个字段集合的并集恰好是 AgentInfo 声明的字段。'
            f' 实际漏归类: {sorted(unclassified)}；AgentInfo 上已不存在: {sorted(stale)}。'
            ' 把新字段归入 UPDATE_WRITABLE_FIELDS（只有一个写者）或 UPDATE_PROTECTED_FIELDS'
            '（另有专用写入机制），并删掉集合里已被移除的字段名。'
        )
        unowned = UPDATE_PROTECTED_FIELDS - UPDATE_PROTECTED_FIELD_OWNERS.keys()
        orphaned = UPDATE_PROTECTED_FIELD_OWNERS.keys() - UPDATE_PROTECTED_FIELDS
        assert not unowned and not orphaned, (
            '期望每个受保护字段都在 UPDATE_PROTECTED_FIELD_OWNERS 里写明哪个操作才能改它。'
            f' 实际没写明: {sorted(unowned)}；已不再受保护却仍留着条目: {sorted(orphaned)}。'
            ' 给每个受保护字段补上它的写入路径，否则拒绝写该字段时只告诉调用方被拒、'
            '不告诉他该改用什么。'
        )

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

    async def test_register_rejects_empty_string_parent_id(self, registry: AgentRegistry) -> None:
        """空串是「声称有父但父未注册」，把它当无父会让子代静默脱离父→子索引。"""
        with _rejects("register 一个 parent_id='' 的 Agent"):
            await registry.register(_info('child', parent_id=''))
        assert await registry.get('child') is None

    async def test_register_stores_snapshot_not_caller_reference(
        self, registry: AgentRegistry
    ) -> None:
        info = _info()
        await registry.register(info)
        info.status = AgentStatus.ABORTING
        info.metadata['leaked'] = True
        got = await registry.get('a1')
        assert got is not None
        assert got.status is AgentStatus.IDLE
        assert got.metadata == {}

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

    async def test_concurrent_transitions_from_the_same_state_have_exactly_one_winner(
        self, registry: AgentRegistry
    ) -> None:
        """同一起点上的并发 CAS 恰好一个赢家。基数取 3：n=2 下有退化解。

        读与写之间插入一次让出（远端 store 必然有的那次往返）必须仍然恰好一个 True，且末态与
        那一个一致。可接受的实现路径：Lua EVAL、WATCH / MULTI / EXEC、或一次事务。
        """
        await registry.register(_info())
        targets = (AgentStatus.RUNNING, AgentStatus.PAUSED, AgentStatus.WAITING)
        results = await asyncio.gather(
            *[
                registry.compare_and_set_status('a1', expected=AgentStatus.IDLE, new=target)
                for target in targets
            ]
        )
        assert results.count(True) == 1
        got = await registry.get('a1')
        assert got is not None
        assert got.status == targets[results.index(True)]

    async def test_update_writes_single_writer_fields(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.update('a1', session_id='s1')
        got = await registry.get('a1')
        assert got is not None and got.session_id == 's1'

    async def test_update_stores_a_snapshot_of_metadata(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        metadata = {'nested': {'labels': ['original']}}
        await registry.update('a1', metadata=metadata)
        metadata['nested']['labels'].append('mutated')  # pyright: ignore[reportAttributeAccessIssue]
        got = await registry.get('a1')
        assert got is not None
        assert got.metadata == {'nested': {'labels': ['original']}}

    async def test_update_copy_failure_writes_no_field(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        try:
            await registry.update(
                'a1',
                session_id='must-not-stick',
                metadata={'bad': _Uncopyable()},
            )
        except Exception:
            pass
        else:
            raise AssertionError(
                '期望不可深拷的 metadata 让 update 失败。'
                ' 实际调用成功返回。'
                ' 在写入前深拷所有字段，失败时不要留下前面字段的部分更新。'
            )
        got = await registry.get('a1')
        assert got is not None
        assert got.session_id is None
        assert got.metadata == {}

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

    async def test_update_rejects_parent_id(self, registry: AgentRegistry) -> None:
        """parent_id 与 children 是同一条链接的两端：单写一端，两边的索引就互相矛盾。"""
        await registry.register(_info('root'))
        await registry.register(_info('other'))
        await registry.register(_info('child', parent_id='root'))
        with _rejects('update 写 parent_id'):
            await registry.update('child', parent_id='other')
        assert await registry.children_of('root') == ['child']
        assert await registry.children_of('other') == []

    async def test_update_rejects_status(self, registry: AgentRegistry) -> None:
        """状态有并发写者；无条件写绕过 CAS，等于允许并发转换互相静默覆盖。"""
        await registry.register(_info())
        with _rejects('update 写 status'):
            await registry.update('a1', status=AgentStatus.RUNNING)
        got = await registry.get('a1')
        assert got is not None and got.status is AgentStatus.IDLE

    async def test_update_rejects_created_at(self, registry: AgentRegistry) -> None:
        """created_at 是构造时戳；无条件写会把「活了多久」改成「刚才改过」。"""
        await registry.register(_info())
        before = await registry.get('a1')
        assert before is not None
        with _rejects('update 写 created_at'):
            await registry.update('a1', created_at=before.created_at + 100.0)
        got = await registry.get('a1')
        assert got is not None and got.created_at == before.created_at

    async def test_rejected_update_writes_no_field(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        with _rejects('update 同时写一个可写字段与一个受保护字段'):
            await registry.update('a1', session_id='s1', status=AgentStatus.RUNNING)
        got = await registry.get('a1')
        assert got is not None and got.session_id is None

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
        listed[0].metadata['leaked'] = True
        again = await registry.list()
        assert again[0].status is AgentStatus.IDLE
        assert again[0].metadata == {}

    async def test_unregister_removes_entry(self, registry: AgentRegistry) -> None:
        await registry.register(_info())
        await registry.unregister('a1')
        assert await registry.get('a1') is None

    async def test_unregister_unknown_agent_is_a_noop(self, registry: AgentRegistry) -> None:
        """「不在注册表里」已经成立，无从表达失败；抛异常会逼调用方先 get 再删。"""
        await registry.unregister('missing')
        assert await registry.get('missing') is None

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
        got.metadata['leaked'] = True
        again = await registry.get('a1')
        assert again is not None
        assert again.status is AgentStatus.IDLE
        assert again.metadata == {}
