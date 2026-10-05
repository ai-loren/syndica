"""ArtifactStore 契约。任何实现都必须通过（spec §13.3 / §4.4）。

随 wheel 发布：第三方写 backend 时继承它、提供 `store` fixture 即可验证合规。契约发布后再加断言是
破坏性变更，所以这里一次钉全：读己所写、`id` 跨版本 / 跨 producer 稳定、版本按登记顺序（不排序不透明
的 version 串）、同 (id, version) 幂等、list 的三种过滤、交付物粘性、并发登记基数 3。

**运行前提**见 `syndica.kernel.contracts.base`。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.context import ArtifactRef
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.protocols.artifact import ArtifactStore

__all__ = ['ArtifactStoreContract']


def _ref(
    artifact_id: str,
    version: str,
    *,
    kind: str = 'document',
    mime: str = 'text/markdown',
    producer: str = 'agent-1',
    is_deliverable: bool = False,
) -> ArtifactRef:
    return ArtifactRef(
        id=artifact_id,
        path=artifact_id.removeprefix('file:'),
        kind=kind,
        mime=mime,
        version=version,
        producer=producer,
        is_deliverable=is_deliverable,
    )


class ArtifactStoreContract(ContractSuite):
    async def test_a_registered_artifact_reads_back(self, store: ArtifactStore) -> None:
        ref = _ref('file:report.md', 'v1')
        await store.register(ref)
        assert await store.get('file:report.md') == ref
        assert await store.versions_of('file:report.md') == [ref]

    async def test_unknown_ids_read_as_empty(self, store: ArtifactStore) -> None:
        assert await store.get('file:nope.md') is None
        assert await store.versions_of('file:nope.md') == []

    async def test_the_same_id_across_producers_is_one_artifact_with_many_versions(
        self, store: ArtifactStore
    ) -> None:
        # id 由路径派生、与谁写无关（§7.2）：Worker 写、Reviewer 改 → 同一个产物的两个版本，各自
        # 保留 producer。把 producer 拼进 id 会在这里变红（评审 B1）。
        first = _ref('file:report.md', 'v1', producer='worker')
        second = _ref('file:report.md', 'v2', producer='reviewer')
        await store.register(first)
        await store.register(second)
        assert await store.versions_of('file:report.md') == [first, second]
        assert await store.get('file:report.md') == second  # get 给最新版本
        assert [r.producer for r in await store.versions_of('file:report.md')] == [
            'worker',
            'reviewer',
        ]

    async def test_versions_are_in_registration_order(self, store: ArtifactStore) -> None:
        # version 不透明（git SHA 不可排序）：顺序是登记顺序。3 个既非升序也非降序的串，`sorted`
        # 变异在这里变红（评审 H1；基数 3，n=2 挡不住排序类变异）。
        versions = ['v-m', 'v-a', 'v-z']
        for v in versions:
            await store.register(_ref('file:a.md', v))
        assert [r.version for r in await store.versions_of('file:a.md')] == versions

    async def test_registering_the_same_id_and_version_twice_is_idempotent(
        self, store: ArtifactStore
    ) -> None:
        ref = _ref('file:a.md', 'v1')
        await store.register(ref)
        await store.register(ref)
        assert await store.versions_of('file:a.md') == [ref]

    async def test_list_gives_one_latest_ref_per_artifact(self, store: ArtifactStore) -> None:
        await store.register(_ref('file:a.md', 'v1'))
        await store.register(_ref('file:a.md', 'v2'))
        await store.register(_ref('file:b.md', 'v1'))
        listed = await store.list()
        assert {r.id: r.version for r in listed} == {'file:a.md': 'v2', 'file:b.md': 'v1'}

    async def test_list_filters_by_kind_and_producer(self, store: ArtifactStore) -> None:
        await store.register(_ref('file:a.md', 'v1', kind='document', producer='worker'))
        await store.register(_ref('file:b.csv', 'v1', kind='spreadsheet', producer='worker'))
        await store.register(_ref('file:c.md', 'v1', kind='document', producer='reviewer'))
        assert {r.id for r in await store.list(kind='document')} == {'file:a.md', 'file:c.md'}
        assert {r.id for r in await store.list(producer='worker')} == {'file:a.md', 'file:b.csv'}
        assert {r.id for r in await store.list(kind='document', producer='worker')} == {'file:a.md'}

    async def test_deliverable_only_filters_and_the_mark_is_sticky(
        self, store: ArtifactStore
    ) -> None:
        # 标过交付物的产物，即便之后又登记一个没带标记的新版本（隐式追踪会这么做），仍算交付物。
        await store.register(_ref('file:draft.md', 'v1'))
        await store.register(_ref('file:report.md', 'v1', is_deliverable=True))
        await store.register(_ref('file:report.md', 'v2', is_deliverable=False))
        assert {r.id for r in await store.list(deliverable_only=True)} == {'file:report.md'}

    async def test_re_registering_the_current_version_marks_it_deliverable(
        self, store: ArtifactStore
    ) -> None:
        # Deliver 的流程：某版本已被隐式追踪登记（非交付物），Deliver 把**同一个版本**（内容没变、
        # 版本号不变）重登记一次、只为置上交付标记。幂等跳过不能把这个标记丢了。
        await store.register(_ref('file:report.md', 'v1', is_deliverable=False))
        await store.register(_ref('file:report.md', 'v1', is_deliverable=True))
        assert {r.id for r in await store.list(deliverable_only=True)} == {'file:report.md'}
        assert await store.versions_of('file:report.md') == [
            _ref('file:report.md', 'v1', is_deliverable=False)
        ]  # 没多出一个版本

    async def test_reverting_to_an_earlier_version_makes_it_current(
        self, store: ArtifactStore
    ) -> None:
        # 版本是内容摘要：A→B→A 改回去之后当前版本是 A，不是已不存在的 B（评审 Medium 7）。
        await store.register(_ref('file:a.md', 'v-a'))
        await store.register(_ref('file:a.md', 'v-b'))
        await store.register(_ref('file:a.md', 'v-a'))
        current = await store.get('file:a.md')
        assert current is not None and current.version == 'v-a'
        assert [r.version for r in await store.versions_of('file:a.md')] == ['v-a', 'v-b', 'v-a']

    async def test_get_and_list_report_the_sticky_deliverable_flag(
        self, store: ArtifactStore
    ) -> None:
        # 交付后又被隐式追踪登记了新版本（没带标记）：交出去的「当前」ref 仍要说它是交付物。
        await store.register(_ref('file:report.md', 'v1', is_deliverable=True))
        await store.register(_ref('file:report.md', 'v2'))
        current = await store.get('file:report.md')
        assert current is not None and current.is_deliverable is True
        assert [r.is_deliverable for r in await store.list()] == [True]

    async def test_three_concurrent_registrations_of_one_id_all_land(
        self, store: ArtifactStore
    ) -> None:
        # 基数 3：同一 id 三个不同版本并发登记，三个都在、无丢失无重复（spec §4.4 并发；n=2 退化）。
        await asyncio.gather(*(store.register(_ref('file:a.md', f'v{i}')) for i in range(3)))
        got = sorted(r.version for r in await store.versions_of('file:a.md'))
        assert got == ['v0', 'v1', 'v2']
