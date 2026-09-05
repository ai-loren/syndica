"""AgentRegistry 契约。任何实现都必须通过。

「可替换」若无测试证明即是空话（spec §13.3）。本套件随发行版发布，第三方
写 backend 时继承它并提供 registry fixture 即可验证合规。

**运行前提**：本套件绝大多数用例是 `async def`，需要 pytest-asyncio 且
`asyncio_mode = "auto"`（或由宿主给用例打上 async 插件的标记）。缺了它，
用例在旧版 pytest 上会被**静默跳过**而非报错——在汇总里与通过无法区分。
`test_async_cases_are_driven_by_an_async_plugin` 就是为此把这种配置缺失变成显式失败。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import fields
from inspect import iscoroutinefunction
from typing import Any

from syndica.kernel.agent_types import AgentInfo, AgentStatus
from syndica.kernel.errors import SyndicaError
from syndica.kernel.protocols.registry import (
    UPDATE_PROTECTED_FIELD_OWNERS,
    UPDATE_PROTECTED_FIELDS,
    UPDATE_WRITABLE_FIELDS,
    AgentRegistry,
)

__all__ = ['AgentRegistryContract']

# 能驱动 async 用例的插件所打的标记；命中任意一个即视为该用例真的会被执行。
_ASYNC_PLUGIN_MARKS = frozenset({'anyio', 'asyncio', 'trio'})


def _info(agent_id: str = 'a1', **kw: object) -> AgentInfo:
    return AgentInfo(
        agent_id=agent_id,
        name='Worker',
        agent_type='declarative',
        **kw,  # pyright: ignore[reportArgumentType]
    )


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


class AgentRegistryContract:
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

    def test_async_cases_are_driven_by_an_async_plugin(self, request: Any) -> None:
        async_items = [
            item
            for item in request.session.items
            if getattr(item, 'cls', None) is type(self)
            and iscoroutinefunction(getattr(item, 'obj', None))
        ]
        # 标记是 collection 期打上的：没被收集的 async 用例根本没有标记可读。因此收集数为零时
        # 「读不到标记」既可能是插件缺失、也可能是 -k / -m / --deselect / --lf / xdist 分片
        # 把它们排除了，两者无从区分——没有证据就不下结论，而不是判失败。
        if not async_items:
            return
        undriven = [
            item.nodeid
            for item in async_items
            if not any(mark.name in _ASYNC_PLUGIN_MARKS for mark in item.iter_markers())
        ]
        assert not undriven, (
            f'期望每个 async 用例都带上 {sorted(_ASYNC_PLUGIN_MARKS)} 之一的标记，'
            ' 即真的有插件驱动它。'
            f' 实际有 {len(undriven)} 个没有，例如 {undriven[0]}。'
            ' 装上 pytest-asyncio 并设 asyncio_mode = "auto"，否则这些用例会被静默跳过，'
            ' 汇总里看起来与通过一模一样。'
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
