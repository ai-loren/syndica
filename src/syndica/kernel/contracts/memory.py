"""MemoryStore 契约。任何实现都必须通过（spec §13.3）。

随 wheel 发布：第三方写 backend 时继承它、提供 `store` fixture 即可验证合规。契约发布后再加断言是
破坏性变更，所以 round-trip 在这里一次钉全：两种 trust、每个字段、两层次隔离、全局忽略 workspace、
seq 单调（并发基数 3）、consolidate 乐观并发冲突显式报。

**运行前提**见 `syndica.kernel.contracts.base`。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.context import TrustLevel
from syndica.kernel.contracts.base import ContractSuite
from syndica.kernel.errors import SyndicaError
from syndica.kernel.memory_redaction import (
    redacted_memory_entry,
    redacted_memory_sections,
)
from syndica.kernel.memory_types import MemoryEntry, MemoryScope, MemorySection
from syndica.kernel.protocols.memory import MemoryStore
from syndica.kernel.redaction import RuleRedactor

__all__ = ['MemoryStoreContract']

_REDACTOR = RuleRedactor()


def _entry(
    *,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    workspace: str | None = '/w',
    text: str = 'remember this',
    trust: TrustLevel = TrustLevel.TRUSTED,
):
    return redacted_memory_entry(
        MemoryEntry(seq=0, scope=scope, workspace=workspace, text=text, trust=trust), _REDACTOR
    )


def _sections(
    *,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    workspace: str | None = '/w',
    through_seq: int = 0,
    sections: list[MemorySection] | None = None,
):
    body = sections or [
        MemorySection(heading='convention', text='use tabs', trust=TrustLevel.TRUSTED),
        MemorySection(heading='from a page', text='claimed X', trust=TrustLevel.UNTRUSTED),
    ]
    return redacted_memory_sections(scope, workspace, body, through_seq, _REDACTOR)


class MemoryStoreContract(ContractSuite):
    """子类必须提供一个 `store` fixture，且每个用例拿到的必须是空存储。"""

    async def test_record_then_entries_roundtrips_every_field(self, store: MemoryStore) -> None:
        await store.record(_entry(text='trusted note', trust=TrustLevel.TRUSTED))
        await store.record(_entry(text='tainted note', trust=TrustLevel.UNTRUSTED))
        entries = await store.entries(MemoryScope.WORKSPACE, '/w')
        assert [e.seq for e in entries] == [1, 2]
        assert [e.text for e in entries] == ['trusted note', 'tainted note']
        # 两个取值都在：一个「总是 TRUSTED」的 fail-open 实现会把第二条洗成可信。
        assert [e.trust for e in entries] == [TrustLevel.TRUSTED, TrustLevel.UNTRUSTED]
        assert all(e.scope is MemoryScope.WORKSPACE and e.workspace == '/w' for e in entries)

    async def test_record_assigns_monotonic_seq(self, store: MemoryStore) -> None:
        seqs = [await store.record(_entry(text=f'n{i}')) for i in range(3)]
        assert seqs == [1, 2, 3]

    async def test_concurrent_record_gets_distinct_seqs(self, store: MemoryStore) -> None:
        # 基数 3：读当前最大 seq 与写新 seq 之间让出的实现会发同一个号，n=2 看不出。
        seqs = await asyncio.gather(*(store.record(_entry(text=f'n{i}')) for i in range(3)))
        assert sorted(seqs) == [1, 2, 3]

    async def test_entries_after_seq_filters(self, store: MemoryStore) -> None:
        for i in range(3):
            await store.record(_entry(text=f'n{i}'))
        later = await store.entries(MemoryScope.WORKSPACE, '/w', after_seq=1)
        assert [e.seq for e in later] == [2, 3]

    async def test_two_levels_are_isolated(self, store: MemoryStore) -> None:
        await store.record(_entry(scope=MemoryScope.WORKSPACE, workspace='/w', text='ws'))
        await store.record(_entry(scope=MemoryScope.GLOBAL, workspace=None, text='global'))
        ws = await store.entries(MemoryScope.WORKSPACE, '/w')
        gl = await store.entries(MemoryScope.GLOBAL, None)
        assert [e.text for e in ws] == ['ws']
        assert [e.text for e in gl] == ['global']

    async def test_workspaces_are_isolated(self, store: MemoryStore) -> None:
        await store.record(_entry(workspace='/a', text='in a'))
        await store.record(_entry(workspace='/b', text='in b'))
        assert [e.text for e in await store.entries(MemoryScope.WORKSPACE, '/a')] == ['in a']
        assert [e.text for e in await store.entries(MemoryScope.WORKSPACE, '/b')] == ['in b']

    async def test_global_ignores_workspace(self, store: MemoryStore) -> None:
        # 全局只有一份：带不带 workspace 都落同一条日志。
        await store.record(_entry(scope=MemoryScope.GLOBAL, workspace='/a', text='one'))
        await store.record(_entry(scope=MemoryScope.GLOBAL, workspace='/b', text='two'))
        both = await store.entries(MemoryScope.GLOBAL, None)
        assert [e.text for e in both] == ['one', 'two']

    async def test_consolidate_then_read_roundtrips(self, store: MemoryStore) -> None:
        version = await store.consolidate(_sections(through_seq=5), expected_version=0)
        assert version == 1
        record = await store.read(MemoryScope.WORKSPACE, '/w')
        assert record is not None
        assert record.through_seq == 5
        assert record.version == 1
        assert [(s.heading, s.text, s.trust) for s in record.sections] == [
            ('convention', 'use tabs', TrustLevel.TRUSTED),
            ('from a page', 'claimed X', TrustLevel.UNTRUSTED),
        ]

    async def test_consolidate_cas_conflict_is_explicit_and_does_not_overwrite(
        self, store: MemoryStore
    ) -> None:
        await store.consolidate(_sections(through_seq=5), expected_version=0)
        clash = _sections(
            through_seq=9,
            sections=[MemorySection(heading='stale', text='should not win')],
        )
        try:
            await store.consolidate(clash, expected_version=0)
        except SyndicaError:
            pass
        else:
            raise AssertionError('consolidate 应在 expected_version 不符时显式报冲突')
        record = await store.read(MemoryScope.WORKSPACE, '/w')
        assert record is not None
        assert record.through_seq == 5  # 冲突未覆盖
        assert record.version == 1

    async def test_consolidate_bumps_version_each_time(self, store: MemoryStore) -> None:
        first = await store.consolidate(_sections(through_seq=1), expected_version=0)
        second = await store.consolidate(_sections(through_seq=2), expected_version=first)
        assert (first, second) == (1, 2)

    async def test_read_unknown_returns_none(self, store: MemoryStore) -> None:
        assert await store.read(MemoryScope.WORKSPACE, '/nope') is None

    async def test_read_hands_back_an_independent_copy(self, store: MemoryStore) -> None:
        await store.consolidate(_sections(through_seq=1), expected_version=0)
        first = await store.read(MemoryScope.WORKSPACE, '/w')
        assert first is not None
        first.sections[0].text = 'mutated the returned copy'
        second = await store.read(MemoryScope.WORKSPACE, '/w')
        assert second is not None
        assert second.sections[0].text == 'use tabs'

    async def test_entries_hands_back_an_independent_copy(self, store: MemoryStore) -> None:
        await store.record(_entry(text='original'))
        first = await store.entries(MemoryScope.WORKSPACE, '/w')
        first[0].text = 'mutated'
        second = await store.entries(MemoryScope.WORKSPACE, '/w')
        assert second[0].text == 'original'
